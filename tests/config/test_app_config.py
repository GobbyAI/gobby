"""Tests for the configuration system."""

import json
import os
import stat
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml
from pydantic import ValidationError

from gobby.config.ai import GenerationConfig
from gobby.config.app import (
    DaemonConfig,
    apply_cli_overrides,
    expand_env_vars,
    export_config_to_yaml,
    load_yaml,
)
from gobby.config.bin_freshness import BinFreshnessConfig
from gobby.config.code_index import CodeIndexConfig
from gobby.config.extensions import (
    HookExtensionsConfig,
    WebhookEndpointConfig,
    WebhooksConfig,
    WebSocketBroadcastConfig,
)
from gobby.config.feature_base import (
    FeatureProfile,
    candidate_labels,
    default_candidates_for_profile,
)
from gobby.config.features import (
    ChatConfig,
    ImportMCPServerConfig,
    MetricsConfig,
    ProjectVerificationConfig,
    ProjectVerificationSynthesisConfig,
    RecommendToolsConfig,
    ToolSummarizerConfig,
)
from gobby.config.hooks import HookTimeoutConfig
from gobby.config.persistence import MemoryBackupConfig, MemoryConfig
from gobby.config.servers import MCPClientProxyConfig, WebSocketSettings
from gobby.config.sessions import (
    MessageTrackingConfig,
    SessionLifecycleConfig,
    SessionSummaryConfig,
)
from gobby.config.tasks import (
    DEFAULT_WORKFLOW_TIMEOUT_SECONDS,
    GobbyTasksConfig,
    TaskExpansionConfig,
    TaskValidationConfig,
    WorkflowConfig,
)
from gobby.config.ui import UIConfig
from gobby.config.wiki import WikiConfig
from gobby.telemetry.config import TelemetrySettings

pytestmark = pytest.mark.unit


def test_code_index_config_maintenance_defaults() -> None:
    config = CodeIndexConfig()

    assert config.maintenance_interval_seconds == 3600
    assert config.maintenance_index_timeout_seconds == 900
    assert config.nightly_repair_enabled is True
    assert config.nightly_repair_cron == "0 2 * * *"
    assert config.nightly_repair_timezone is None
    assert config.nightly_repair_timeout_seconds == 8 * 60 * 60
    assert config.nightly_repair_concurrency == 1
    assert config.maintenance_log_file == "~/.gobby/logs/code-index-maintenance.log"


def test_wiki_config_codewiki_scope_defaults() -> None:
    config = WikiConfig()

    assert config.codewiki_scopes == []
    assert config.codewiki_project_scopes_by_name == {}


@pytest.mark.parametrize(
    "kwargs",
    [
        {"codewiki_scopes": "src"},
        {"codewiki_scopes": ["src", ""]},
        {"codewiki_scopes": ["src", 1]},
        {"codewiki_project_scopes_by_name": ["gobby"]},
        {"codewiki_project_scopes_by_name": {"": ["src"]}},
        {"codewiki_project_scopes_by_name": {"gobby": "src"}},
        {"codewiki_project_scopes_by_name": {"gobby": ["src", None]}},
    ],
)
def test_wiki_config_rejects_malformed_codewiki_scope_config(kwargs: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        WikiConfig(**kwargs)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"nightly_repair_cron": "not a cron"},
        {"nightly_repair_timezone": "Not/AZone"},
    ],
)
def test_code_index_config_rejects_invalid_nightly_schedule(kwargs: dict[str, str]) -> None:
    with pytest.raises(ValidationError):
        CodeIndexConfig(**kwargs)


def test_code_index_config_rejects_retired_nightly_full_reindex_keys() -> None:
    with pytest.raises(ValidationError):
        CodeIndexConfig.model_validate({"nightly_full_reindex_enabled": True})


def write_secure_bootstrap(path: Path, content: str) -> None:
    if (
        "files_home:" not in content
        and "hub_daemon_url:" not in content
        and "datastore_mode: remote" not in content
    ):
        files_home = path.parent / "files"
        files_home.mkdir(exist_ok=True)
        content = f"{content}files_home: {files_home}\n"
    elif "datastore_mode: remote" in content and "hub_daemon_url:" not in content:
        content = f"{content}hub_daemon_url: http://hub.example.test:60887\n"
    path.write_text(content)
    path.chmod(0o600)


class TestExpandEnvVars:
    """Tests for expand_env_vars function."""

    def test_expand_simple_env_var(self) -> None:
        """Test simple ${VAR} expansion."""
        with patch.dict(os.environ, {"MY_VAR": "hello"}):
            result = expand_env_vars("value: ${MY_VAR}")
            assert result == "value: hello"

    def test_expand_with_default_when_var_set(self) -> None:
        """Test ${VAR:-default} uses VAR value when set."""
        with patch.dict(os.environ, {"MY_VAR": "actual_value"}):
            result = expand_env_vars("value: ${MY_VAR:-default_value}")
            assert result == "value: actual_value"

    def test_expand_with_default_when_var_unset(self) -> None:
        """Test ${VAR:-default} uses default when VAR is unset."""
        # Ensure the var is not set
        env = os.environ.copy()
        env.pop("UNSET_VAR", None)
        with patch.dict(os.environ, env, clear=True):
            result = expand_env_vars("value: ${UNSET_VAR:-fallback}")
            assert result == "value: fallback"

    def test_expand_with_default_when_var_empty(self) -> None:
        """Test ${VAR:-default} uses default when VAR is empty string."""
        with patch.dict(os.environ, {"EMPTY_VAR": ""}):
            result = expand_env_vars("value: ${EMPTY_VAR:-fallback}")
            assert result == "value: fallback"

    def test_expand_simple_var_unset_leaves_unchanged(self) -> None:
        """Test simple ${VAR} is left unchanged when VAR is unset."""
        env = os.environ.copy()
        env.pop("UNDEFINED_VAR", None)
        with patch.dict(os.environ, env, clear=True):
            result = expand_env_vars("value: ${UNDEFINED_VAR}")
            assert result == "value: ${UNDEFINED_VAR}"

    def test_expand_multiple_vars(self) -> None:
        """Test expanding multiple variables in one string."""
        with patch.dict(os.environ, {"VAR1": "first", "VAR2": "second"}):
            result = expand_env_vars("a: ${VAR1}, b: ${VAR2:-def}")
            assert result == "a: first, b: second"

    def test_expand_no_vars(self) -> None:
        """Test string without env vars is unchanged."""
        result = expand_env_vars("plain text without variables")
        assert result == "plain text without variables"

    def test_expand_empty_default(self) -> None:
        """Test ${VAR:-} uses empty string as default."""
        env = os.environ.copy()
        env.pop("UNSET_VAR", None)
        with patch.dict(os.environ, env, clear=True):
            result = expand_env_vars("value: ${UNSET_VAR:-}")
            assert result == "value: "

    def test_secret_resolver_takes_priority_over_env(self) -> None:
        """Test that secret_resolver is checked before env vars."""

        def resolver(name: str) -> str | None:
            return "secret_value" if name == "MY_KEY" else None

        with patch.dict(os.environ, {"MY_KEY": "env_value"}):
            result = expand_env_vars("key: ${MY_KEY}", secret_resolver=resolver)
            assert result == "key: secret_value"

    def test_secret_resolver_fallback_to_env(self) -> None:
        """Test that env var is used when secret_resolver returns None."""

        def resolver(name: str) -> str | None:
            return None

        with patch.dict(os.environ, {"MY_KEY": "env_value"}):
            result = expand_env_vars("key: ${MY_KEY}", secret_resolver=resolver)
            assert result == "key: env_value"

    def test_secret_resolver_fallback_to_default(self) -> None:
        """Test that default is used when both resolver and env return nothing."""

        def resolver(name: str) -> str | None:
            return None

        env = os.environ.copy()
        env.pop("UNSET_VAR", None)
        with patch.dict(os.environ, env, clear=True):
            result = expand_env_vars("key: ${UNSET_VAR:-fallback}", secret_resolver=resolver)
            assert result == "key: fallback"

    def test_secret_resolver_unresolved_warns(self) -> None:
        """Test that unresolved vars log a warning."""

        def resolver(name: str) -> str | None:
            return None

        env = os.environ.copy()
        env.pop("MISSING_VAR", None)
        with patch.dict(os.environ, env, clear=True):
            with patch("gobby.config._loading.logger") as mock_logger:
                result = expand_env_vars("key: ${MISSING_VAR}", secret_resolver=resolver)
                assert result == "key: ${MISSING_VAR}"
                mock_logger.warning.assert_called_once()
                warning_args = mock_logger.warning.call_args.args
                assert "MISSING_VAR" in warning_args[0] % warning_args[1:]

    def test_secret_resolver_exception_falls_through(self) -> None:
        """Test that secret_resolver exceptions are caught and fall through to env."""

        def bad_resolver(name: str) -> str | None:
            raise RuntimeError("DB unavailable")

        with patch.dict(os.environ, {"MY_KEY": "env_value"}):
            result = expand_env_vars("key: ${MY_KEY}", secret_resolver=bad_resolver)
            assert result == "key: env_value"

    def test_unresolved_var_warns_without_resolver(self) -> None:
        """Test that unresolved vars warn even without a secret_resolver."""
        env = os.environ.copy()
        env.pop("NOPE", None)
        with patch.dict(os.environ, env, clear=True):
            with patch("gobby.config._loading.logger") as mock_logger:
                result = expand_env_vars("key: ${NOPE}")
                assert result == "key: ${NOPE}"
                mock_logger.warning.assert_called_once()

    def test_secret_ref_resolved(self) -> None:
        """Test $secret:NAME resolves from secrets store only."""

        def resolver(name: str) -> str | None:
            return "my_secret" if name == "API_KEY" else None

        result = expand_env_vars("key: $secret:API_KEY", secret_resolver=resolver)
        assert result == "key: my_secret"

    def test_secret_values_are_not_reexpanded(self) -> None:
        """Secret values are returned as-is instead of being interpolated again."""

        def resolver(name: str) -> str | None:
            if name == "API_KEY":
                return "$secret:OTHER ${ENV_KEY}"
            if name == "OTHER":
                return "other_secret"
            return None

        with patch.dict(os.environ, {"ENV_KEY": "env_value"}):
            result = expand_env_vars(
                "env: ${API_KEY}, direct: $secret:API_KEY",
                secret_resolver=resolver,
            )

        assert result == "env: $secret:OTHER ${ENV_KEY}, direct: $secret:OTHER ${ENV_KEY}"

    def test_secret_ref_no_env_fallback(self) -> None:
        """Test $secret:NAME does NOT fall back to env vars."""

        def resolver(name: str) -> str | None:
            return None

        with patch.dict(os.environ, {"API_KEY": "env_value"}):
            with patch("gobby.config._loading.logger"):
                result = expand_env_vars("key: $secret:API_KEY", secret_resolver=resolver)
                assert result == "key: $secret:API_KEY"  # Left unchanged, no env fallback

    def test_secret_ref_without_resolver_unchanged(self) -> None:
        """Test $secret:NAME is left unchanged when no resolver provided."""
        result = expand_env_vars("key: $secret:API_KEY")
        assert result == "key: $secret:API_KEY"

    def test_secret_ref_warns_on_missing(self) -> None:
        """Test $secret:NAME logs warning when not found."""

        def resolver(name: str) -> str | None:
            return None

        with patch("gobby.config._loading.logger") as mock_logger:
            result = expand_env_vars("key: $secret:MISSING", secret_resolver=resolver)
            assert result == "key: $secret:MISSING"
            mock_logger.warning.assert_called_once()
            warning_args = mock_logger.warning.call_args.args
            assert "MISSING" in warning_args[0] % warning_args[1:]

    def test_mixed_secret_ref_and_env_var(self) -> None:
        """Test $secret:NAME and ${VAR} in same content."""

        def resolver(name: str) -> str | None:
            return "secret_val" if name == "SECRET_KEY" else None

        with patch.dict(os.environ, {"ENV_KEY": "env_val"}):
            result = expand_env_vars(
                "a: $secret:SECRET_KEY, b: ${ENV_KEY}",
                secret_resolver=resolver,
            )
            assert result == "a: secret_val, b: env_val"


class TestWebSocketSettings:
    """Tests for WebSocketSettings configuration."""

    def test_default_values(self) -> None:
        """Test default WebSocket settings."""
        settings = WebSocketSettings()
        assert settings.enabled is True
        assert settings.port == 60888
        assert settings.ping_interval == 30
        assert settings.ping_timeout == 10

    def test_custom_values(self) -> None:
        """Test custom WebSocket settings."""
        settings = WebSocketSettings(
            enabled=False,
            port=9000,
            ping_interval=60,
            ping_timeout=20,
        )
        assert settings.enabled is False
        assert settings.port == 9000
        assert settings.ping_interval == 60
        assert settings.ping_timeout == 20

    def test_port_validation_too_low(self) -> None:
        """Test port validation rejects ports below 1024."""
        with pytest.raises(ValidationError):
            WebSocketSettings(port=80)

    def test_port_validation_too_high(self) -> None:
        """Test port validation rejects ports above 65535."""
        with pytest.raises(ValidationError):
            WebSocketSettings(port=70000)

    def test_ping_interval_must_be_positive(self) -> None:
        """Test ping_interval must be positive."""
        with pytest.raises(ValidationError):
            WebSocketSettings(ping_interval=0)


class TestSessionSummaryConfig:
    """Tests for SessionSummaryConfig."""

    def test_default_values(self) -> None:
        """Test default session summary config."""
        config = SessionSummaryConfig()
        assert config.enabled is True
        assert config.profile == FeatureProfile.LOW
        assert "claude/haiku" in candidate_labels(config.candidates)
        # prompt now has a default template with placeholders
        assert config.prompt is not None
        assert "Generate a concise session summary" in config.prompt
        assert "## Current State" in config.prompt
        assert "## Next Steps" in config.prompt

    def test_custom_values(self) -> None:
        """Test custom session summary config."""
        config = SessionSummaryConfig(
            enabled=False,
            profile=FeatureProfile.MID,
            candidates=["codex/gpt-5.4-mini"],
            prompt="Custom prompt",
        )
        assert config.enabled is False
        assert config.profile == FeatureProfile.MID
        assert candidate_labels(config.candidates) == ("codex/gpt-5.4-mini",)
        assert config.prompt == "Custom prompt"


class TestMCPClientProxyConfig:
    """Tests for MCPClientProxyConfig."""

    def test_default_values(self) -> None:
        """Test default MCP client proxy config."""
        config = MCPClientProxyConfig()
        assert config.enabled is True
        assert config.connect_timeout == 30.0
        assert config.proxy_timeout == 30
        assert config.tool_timeout == 30

    def test_connect_timeout_custom(self) -> None:
        """Test connect_timeout can be customized."""
        config = MCPClientProxyConfig(connect_timeout=60.0)
        assert config.connect_timeout == 60.0

    def test_connect_timeout_validation(self) -> None:
        """Test connect_timeout must be positive."""
        with pytest.raises(ValidationError):
            MCPClientProxyConfig(connect_timeout=0)

        with pytest.raises(ValidationError):
            MCPClientProxyConfig(connect_timeout=-5.0)

    def test_timeout_validation(self) -> None:
        """Test timeouts must be positive."""
        with pytest.raises(ValidationError):
            MCPClientProxyConfig(proxy_timeout=0)

        with pytest.raises(ValidationError):
            MCPClientProxyConfig(tool_timeout=-1)


class TestUIConfig:
    """Tests for UIConfig."""

    def test_default_mode_is_auto(self) -> None:
        config = UIConfig()
        assert config.mode == "auto"

    @pytest.mark.parametrize("mode", ["auto", "dev", "production"])
    def test_mode_validation_accepts_supported_modes(self, mode: str) -> None:
        config = UIConfig(mode=mode)
        assert config.mode == mode

    def test_mode_validation_rejects_invalid_mode(self) -> None:
        with pytest.raises(ValidationError):
            UIConfig(mode="invalid")


class TestDaemonConfig:
    """Tests for DaemonConfig."""

    def test_default_values(self) -> None:
        """Test default daemon config."""
        config = DaemonConfig()
        assert config.daemon_port == 60887
        assert config.daemon_health_check_interval == 10.0
        assert isinstance(config.bin_freshness, BinFreshnessConfig)
        assert isinstance(config.project_verification_synthesis, ProjectVerificationSynthesisConfig)
        assert "conductor" not in DaemonConfig.model_fields
        assert not hasattr(config, "conductor")

    def test_port_validation(self) -> None:
        """Test daemon port validation."""
        with pytest.raises(ValidationError):
            DaemonConfig(daemon_port=80)

        with pytest.raises(ValidationError):
            DaemonConfig(daemon_port=70000)

    def test_health_check_interval_validation(self) -> None:
        """Test health check interval validation."""
        with pytest.raises(ValidationError):
            DaemonConfig(daemon_health_check_interval=0.5)

        with pytest.raises(ValidationError):
            DaemonConfig(daemon_health_check_interval=500.0)

    @pytest.mark.unit
    def test_coordinated_hook_timeout_defaults(self) -> None:
        config = DaemonConfig()

        assert config.workflow.timeout == 90
        assert config.hooks.adapter_timeout == 105
        assert config.hooks.provider_timeout == 120
        assert config.hooks.additional_context_limit == 9_950
        assert config.hooks.additional_context_limits == {}

    @pytest.mark.parametrize(
        ("overrides", "message"),
        [
            ({"workflow": {"timeout": 105}}, "workflow.timeout"),
            ({"hooks": {"adapter_timeout": 120}}, "hooks.adapter_timeout"),
        ],
    )
    @pytest.mark.unit
    def test_hook_timeout_policy_requires_strict_order(
        self,
        overrides: dict[str, object],
        message: str,
    ) -> None:
        with pytest.raises(ValidationError, match=message):
            DaemonConfig(**overrides)

    @pytest.mark.parametrize("field", ["adapter_timeout", "provider_timeout"])
    @pytest.mark.unit
    def test_hook_timeout_values_must_be_positive(self, field: str) -> None:
        with pytest.raises(ValidationError):
            HookTimeoutConfig(**{field: 0})

    def test_sub_config_access(self) -> None:
        """Test accessing sub-configurations."""
        config = DaemonConfig()
        assert config.get_import_mcp_server_config() is config.import_mcp_server
        assert config.get_gobby_tasks_config() is config.gobby_tasks

    def test_get_verification_defaults(self) -> None:
        """Test get_verification_defaults returns verification_defaults config."""
        config = DaemonConfig()
        verification_config = config.get_verification_defaults()
        assert verification_config is config.verification_defaults

        assert isinstance(verification_config, ProjectVerificationConfig)

    def test_rejects_removed_conductor_section(self) -> None:
        """Stale top-level conductor config should fail loudly."""
        with pytest.raises(ValidationError, match="conductor config has been removed"):
            DaemonConfig(conductor={"enabled": False})

    def test_rejects_removed_memory_sync_section(self) -> None:
        with pytest.raises(
            ValidationError,
            match="memory_sync config has been removed. Use memory_backup instead",
        ):
            DaemonConfig(memory_sync={"enabled": True})


class TestLoadYaml:
    """Tests for load_yaml function."""

    def test_load_yaml_file(self, temp_dir: Path) -> None:
        """Test loading YAML file."""
        config_file = temp_dir / "config.yaml"
        config_file.write_text(yaml.dump({"daemon_port": 9000, "logging": {"level": "debug"}}))

        data = load_yaml(str(config_file))
        assert data["daemon_port"] == 9000
        assert data["logging"]["level"] == "debug"

    def test_load_json_file(self, temp_dir: Path) -> None:
        """Test loading JSON file."""
        config_file = temp_dir / "config.json"
        config_file.write_text(json.dumps({"daemon_port": 9001}))

        data = load_yaml(str(config_file))
        assert data["daemon_port"] == 9001

    def test_load_nonexistent_file(self, temp_dir: Path) -> None:
        """Test loading nonexistent file returns empty dict."""
        data = load_yaml(str(temp_dir / "nonexistent.yaml"))
        assert data == {}

    def test_load_empty_file(self, temp_dir: Path) -> None:
        """Test loading empty file returns empty dict."""
        config_file = temp_dir / "empty.yaml"
        config_file.write_text("")

        data = load_yaml(str(config_file))
        assert data == {}

    def test_invalid_extension(self, temp_dir: Path) -> None:
        """Test invalid file extension raises error."""
        config_file = temp_dir / "config.txt"
        config_file.write_text("key: value")

        with pytest.raises(ValueError, match="extension"):
            load_yaml(str(config_file))

    def test_invalid_yaml(self, temp_dir: Path) -> None:
        """Test invalid YAML raises error."""
        config_file = temp_dir / "invalid.yaml"
        config_file.write_text("invalid: yaml: content: [")

        with pytest.raises(ValueError, match="Invalid YAML"):
            load_yaml(str(config_file))

    def test_invalid_json(self, temp_dir: Path) -> None:
        """Test invalid JSON raises error."""
        config_file = temp_dir / "invalid.json"
        config_file.write_text('{"key": "value"')  # Missing closing brace

        with pytest.raises(ValueError, match="Invalid JSON"):
            load_yaml(str(config_file))

    def test_yaml_requires_top_level_mapping(self, temp_dir: Path) -> None:
        """Test YAML lists are rejected as daemon configs."""
        config_file = temp_dir / "list.yaml"
        config_file.write_text("- daemon_port\n- 9000\n")

        with pytest.raises(ValueError, match="mapping/object"):
            load_yaml(str(config_file))

    def test_json_requires_top_level_mapping(self, temp_dir: Path) -> None:
        """Test JSON arrays are rejected as daemon configs."""
        config_file = temp_dir / "list.json"
        config_file.write_text("[1, 2, 3]")

        with pytest.raises(ValueError, match="mapping/object"):
            load_yaml(str(config_file))

    def test_empty_json_file(self, temp_dir: Path) -> None:
        """Test loading empty JSON file returns empty dict."""
        config_file = temp_dir / "empty.json"
        config_file.write_text("")

        data = load_yaml(str(config_file))
        assert data == {}

    def test_env_var_expansion_in_yaml(
        self, temp_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Test environment variable expansion in YAML files."""
        monkeypatch.delenv("TEST_PORT", raising=False)

        config_file = temp_dir / "env_config.yaml"
        config_file.write_text("daemon_port: ${TEST_PORT:-9999}")

        data = load_yaml(str(config_file))
        assert data["daemon_port"] == 9999


class TestApplyCliOverrides:
    """Tests for apply_cli_overrides function."""

    def test_simple_override(self) -> None:
        """Test simple key override."""
        config = {"daemon_port": 60887}
        overrides = {"daemon_port": 9000}

        result = apply_cli_overrides(config, overrides)
        assert result["daemon_port"] == 9000

    def test_nested_override(self) -> None:
        """Test nested key override with dot notation."""
        config = {"logging": {"level": "info"}}
        overrides = {"logging.level": "debug"}

        result = apply_cli_overrides(config, overrides)
        assert result["logging"]["level"] == "debug"

    def test_creates_nested_path(self) -> None:
        """Test creating nested path that doesn't exist."""
        config: dict[str, object] = {}
        overrides = {"logging.level": "debug"}

        result = apply_cli_overrides(config, overrides)
        assert result["logging"]["level"] == "debug"

    def test_none_overrides(self) -> None:
        """Test None overrides returns config unchanged."""
        config = {"key": "value"}
        result = apply_cli_overrides(config, None)
        assert result == config


@pytest.mark.no_config_protection
class TestBootstrapConfig:
    """Tests for bootstrap configuration loading."""

    def test_load_defaults_when_missing(self, temp_dir: Path) -> None:
        """Test bootstrap returns defaults when file is missing."""
        from gobby.config.bootstrap import load_bootstrap

        bootstrap = load_bootstrap(str(temp_dir / "nonexistent.yaml"))
        assert bootstrap.daemon_port == 60887
        assert bootstrap.bind_host == "localhost"
        assert bootstrap.websocket_port == 60888
        assert bootstrap.ui_port == 60889

    def test_load_from_yaml(self, temp_dir: Path) -> None:
        """Test bootstrap reads values from YAML file."""
        from gobby.config.bootstrap import load_bootstrap

        bootstrap_file = temp_dir / "bootstrap.yaml"
        write_secure_bootstrap(
            bootstrap_file,
            "daemon_port: 9999\nbind_host: 0.0.0.0\nwebsocket_port: 9998\nui_port: 9997\n",
        )
        bootstrap = load_bootstrap(str(bootstrap_file))
        assert bootstrap.daemon_port == 9999
        assert bootstrap.bind_host == "0.0.0.0"
        assert bootstrap.websocket_port == 9998
        assert bootstrap.ui_port == 9997

    def test_load_bootstrap_ignores_legacy_neo4j_password(self, temp_dir: Path) -> None:
        """Legacy bootstrap neo4j_password does not affect FalkorDB credentials."""
        from gobby.config.bootstrap import load_bootstrap

        bootstrap_file = temp_dir / "bootstrap.yaml"
        write_secure_bootstrap(bootstrap_file, "neo4j_password: old-secret\n")

        bootstrap = load_bootstrap(str(bootstrap_file))

        assert not hasattr(bootstrap, "falkordb_password")

    def test_to_config_dict(self) -> None:
        """Test bootstrap converts to DaemonConfig-compatible dict."""
        from gobby.config.bootstrap import BootstrapConfig

        bootstrap = BootstrapConfig(daemon_port=7777, websocket_port=7778)
        d = bootstrap.to_config_dict()
        assert d["daemon_port"] == 7777
        assert d["websocket"]["port"] == 7778
        assert d["bind_host"] == "localhost"
        assert "hub_backend" not in d
        assert "files_home" in d
        assert "hub_daemon_url" in d
        carried = DaemonConfig.model_validate(d)
        assert carried.files_home is None
        assert carried.hub_daemon_url is None
        assert d["database_url"] is None
        assert "postgres_install_mode" not in d

    def test_partial_yaml(self, temp_dir: Path) -> None:
        """Test bootstrap fills defaults for missing fields."""
        from gobby.config.bootstrap import load_bootstrap

        bootstrap_file = temp_dir / "bootstrap.yaml"
        write_secure_bootstrap(bootstrap_file, "daemon_port: 5555\n")
        bootstrap = load_bootstrap(str(bootstrap_file))
        assert bootstrap.daemon_port == 5555

    def test_legacy_config_path_redirects(self, temp_dir: Path) -> None:
        """Test that passing a config.yaml path finds bootstrap.yaml in same dir."""
        from gobby.config.bootstrap import load_bootstrap

        bootstrap_file = temp_dir / "bootstrap.yaml"
        write_secure_bootstrap(bootstrap_file, "daemon_port: 4444\n")
        # Pass legacy config.yaml path — should find bootstrap.yaml instead
        bootstrap = load_bootstrap(str(temp_dir / "config.yaml"))
        assert bootstrap.daemon_port == 4444


@pytest.mark.no_config_protection
class TestSaveConfig:
    """Tests for export_config_to_yaml function."""

    def test_saves_config(self, temp_dir: Path, default_config: DaemonConfig) -> None:
        """Test saving config to file."""
        config_file = temp_dir / "saved.yaml"
        export_config_to_yaml(default_config, str(config_file))

        assert config_file.exists()
        content = yaml.safe_load(config_file.read_text())
        assert content["daemon_port"] == default_config.daemon_port

    def test_masks_voice_audio_api_keys_without_hiding_binding_fields(self, temp_dir: Path) -> None:
        config = DaemonConfig(
            voice={
                "openai_compatible_audio": [
                    {
                        "provider": "remote-stt",
                        "url": "https://audio.example/v1",
                        "model": "whisper-large-v3",
                        "api_key": "resolved-runtime-key",
                    }
                ]
            }
        )
        config_file = temp_dir / "voice.yaml"

        export_config_to_yaml(config, str(config_file))

        raw_text = config_file.read_text()
        binding = yaml.safe_load(raw_text)["voice"]["openai_compatible_audio"][0]
        assert binding["api_key"] == "********"
        assert binding["provider"] == "remote-stt"
        assert binding["model"] == "whisper-large-v3"
        assert "resolved-runtime-key" not in raw_text

    def test_masks_all_reference_secrecy_fields(self, temp_dir: Path) -> None:
        plaintext = "legacy-plaintext-must-never-export"
        config = DaemonConfig(
            ai={
                "generation": {
                    "endpoints": {
                        "remote": {
                            "api_base": "https://example.invalid/v1",
                            "api_key": plaintext,
                            "model": "model-a",
                        }
                    }
                }
            }
        )
        config_file = temp_dir / "masked.yaml"

        export_config_to_yaml(config, str(config_file))

        raw_text = config_file.read_text()
        endpoint = yaml.safe_load(raw_text)["ai"]["generation"]["endpoints"]["remote"]
        assert endpoint["api_key"] == "********"
        assert plaintext not in raw_text

    def test_file_permissions(self, temp_dir: Path, default_config: DaemonConfig) -> None:
        """Test saved config has restrictive permissions."""
        config_file = temp_dir / "secure.yaml"
        export_config_to_yaml(default_config, str(config_file))

        # Check permissions (0o600 = owner read/write only)
        mode = config_file.stat().st_mode & 0o777
        assert mode == 0o600

    def test_creates_parent_directory(self, temp_dir: Path, default_config: DaemonConfig) -> None:
        """Test creating parent directory when saving."""
        config_file = temp_dir / "nested" / "dir" / "config.yaml"
        export_config_to_yaml(default_config, str(config_file))

        assert config_file.exists()

    def test_partial_serialization_failure_preserves_existing_export(
        self,
        temp_dir: Path,
        default_config: DaemonConfig,
    ) -> None:
        config_file = temp_dir / "saved.yaml"
        original = b"daemon_port: 12345\n"
        config_file.write_bytes(original)

        def fail_after_partial_write(*args: object, **kwargs: object) -> None:
            stream = args[1]
            assert hasattr(stream, "write")
            stream.write("partial: true\n")
            raise OSError("forced serialization failure")

        with (
            patch("gobby.config._loading.yaml.safe_dump", side_effect=fail_after_partial_write),
            pytest.raises(OSError, match="forced serialization failure"),
        ):
            export_config_to_yaml(default_config, str(config_file))

        assert config_file.read_bytes() == original
        assert list(temp_dir.glob(f".{config_file.name}.*.tmp")) == []

    def test_successful_export_fsyncs_file_then_replacement_directory(
        self,
        temp_dir: Path,
        default_config: DaemonConfig,
    ) -> None:
        config_file = temp_dir / "saved.yaml"
        config_file.write_text("old: true\n")
        real_fsync = os.fsync
        real_replace = os.replace
        events: list[tuple[str, bool]] = []

        def record_fsync(fd: int) -> None:
            events.append(("fsync", stat.S_ISDIR(os.fstat(fd).st_mode)))
            real_fsync(fd)

        def record_replace(source: str | os.PathLike[str], target: str | os.PathLike[str]) -> None:
            events.append(("replace", False))
            real_replace(source, target)

        with (
            patch("gobby.config._loading.os.fsync", side_effect=record_fsync),
            patch("gobby.config._loading.os.replace", side_effect=record_replace),
        ):
            export_config_to_yaml(default_config, str(config_file))

        assert events == [("fsync", False), ("replace", False), ("fsync", True)]
        assert yaml.safe_load(config_file.read_text())["daemon_port"] == default_config.daemon_port
        assert list(temp_dir.glob(f".{config_file.name}.*.tmp")) == []

    def test_export_config_to_yaml_with_none_path_uses_default(
        self,
        temp_dir: Path,
        default_config: DaemonConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Test saving config with config_file=None uses default path."""
        monkeypatch.delenv("GOBBY_TEST_PROTECT", raising=False)

        # Patch expanduser to redirect ~/.gobby to temp_dir/.gobby
        original_expanduser = Path.expanduser

        def mock_expanduser(self: Path) -> Path:
            path_str = str(self)
            if path_str.startswith("~/.gobby"):
                return temp_dir / ".gobby" / path_str[9:]  # Remove ~/.gobby/
            return original_expanduser(self)

        monkeypatch.setattr(Path, "expanduser", mock_expanduser)

        export_config_to_yaml(default_config, config_file=None)

        # Check the file was saved to the mocked default path
        expected_path = temp_dir / ".gobby" / "config.yaml"
        assert expected_path.exists()


@pytest.mark.no_config_protection
class TestSaveConfigTestGuard:
    """Tests for export_config_to_yaml GOBBY_TEST_PROTECT guard."""

    def test_raises_when_test_protect_set_and_no_path(
        self, default_config: DaemonConfig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """export_config_to_yaml raises RuntimeError when GOBBY_TEST_PROTECT=1 and config_file is None."""
        monkeypatch.setenv("GOBBY_TEST_PROTECT", "1")

        with pytest.raises(
            RuntimeError,
            match="export_config_to_yaml.*would write to production path.*during tests",
        ):
            export_config_to_yaml(default_config, config_file=None)

    def test_no_error_with_explicit_path(
        self,
        temp_dir: Path,
        default_config: DaemonConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """export_config_to_yaml works with explicit path even when GOBBY_TEST_PROTECT=1."""
        monkeypatch.setenv("GOBBY_TEST_PROTECT", "1")

        config_file = temp_dir / "safe.yaml"
        export_config_to_yaml(default_config, str(config_file))
        assert config_file.exists()

    def test_no_error_without_test_protect(
        self,
        temp_dir: Path,
        default_config: DaemonConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """export_config_to_yaml works normally when GOBBY_TEST_PROTECT is not set."""
        monkeypatch.delenv("GOBBY_TEST_PROTECT", raising=False)

        # Redirect expanduser so we don't touch real config
        original_expanduser = Path.expanduser

        def mock_expanduser(self: Path) -> Path:
            path_str = str(self)
            if path_str.startswith("~/.gobby"):
                return temp_dir / ".gobby" / path_str[9:]
            return original_expanduser(self)

        monkeypatch.setattr(Path, "expanduser", mock_expanduser)

        export_config_to_yaml(default_config, config_file=None)
        assert (temp_dir / ".gobby" / "config.yaml").exists()


class TestRecommendToolsConfig:
    """Tests for RecommendToolsConfig."""

    def test_default_values(self) -> None:
        """Test default recommend tools config."""
        config = RecommendToolsConfig()
        assert config.enabled is True
        assert config.profile == FeatureProfile.MID
        assert "claude/sonnet" in candidate_labels(config.candidates)
        assert config.prompt_path is None  # Uses default prompt from prompts/


class TestImportMCPServerConfig:
    """Tests for ImportMCPServerConfig."""

    def test_default_values(self) -> None:
        """Test default import MCP server config."""
        config = ImportMCPServerConfig()
        assert config.enabled is True
        assert config.profile == FeatureProfile.LOW
        assert "claude/haiku" in candidate_labels(config.candidates)
        assert config.prompt_path is None  # Uses DEFAULT_IMPORT_MCP_SERVER_PROMPT


class TestProjectVerificationConfig:
    """Tests for ProjectVerificationConfig."""

    def test_build_and_doc_tests_are_first_class_commands(self) -> None:
        """build and doc_tests load and appear in command lookups."""
        config = ProjectVerificationConfig(
            build="uv build",
            doc_tests="uv run pytest --doctest-modules src/",
            custom={"frontend_tests": "cd web && npm test"},
        )

        assert config.get_command("build") == "uv build"
        assert config.get_command("doc_tests") == "uv run pytest --doctest-modules src/"
        assert config.all_commands()["build"] == "uv build"
        assert config.all_commands()["doc_tests"] == "uv run pytest --doctest-modules src/"
        assert config.all_commands()["frontend_tests"] == "cd web && npm test"


class TestProjectVerificationSynthesisConfig:
    """Tests for ProjectVerificationSynthesisConfig."""

    def test_default_values(self) -> None:
        """Default synthesis uses feature_mid with a 0.75 threshold."""
        config = ProjectVerificationSynthesisConfig()

        assert config.profile == FeatureProfile.MID
        assert config.confidence_threshold == 0.75
        assert candidate_labels(config.candidates) == default_candidates_for_profile(
            FeatureProfile.MID
        )


class TestWebSocketBroadcastConfig:
    """Tests for WebSocketBroadcastConfig."""

    def test_default_values(self) -> None:
        """Test default WebSocket broadcast config."""
        config = WebSocketBroadcastConfig()
        assert config.enabled is True
        assert "session-start" in config.broadcast_events
        assert config.include_payload is True


class TestHookExtensionsConfig:
    """Tests for HookExtensionsConfig."""

    def test_default_values(self) -> None:
        """Test default hook extensions config."""
        config = HookExtensionsConfig()
        assert isinstance(config.websocket, WebSocketBroadcastConfig)


class TestTaskExpansionConfig:
    """Tests for TaskExpansionConfig."""

    def test_default_values(self) -> None:
        """Test default task expansion config."""
        config = TaskExpansionConfig()
        assert config.enabled is True
        assert config.profile == FeatureProfile.HIGH
        assert candidate_labels(config.candidates) == default_candidates_for_profile(
            FeatureProfile.HIGH
        )
        assert config.prompt_path is None  # Uses default prompt from prompts/


class TestTaskValidationConfig:
    """Tests for TaskValidationConfig."""

    def test_default_values(self) -> None:
        """Test default task validation config."""
        config = TaskValidationConfig()
        assert config.enabled is True
        assert config.profile == FeatureProfile.MID
        assert "claude/sonnet" in candidate_labels(config.candidates)
        assert "cli_candidate_timeout_seconds" not in TaskValidationConfig.model_fields
        assert "tool_loop_enabled" not in TaskValidationConfig.model_fields
        assert "tool_loop_preview_bytes" not in TaskValidationConfig.model_fields
        assert "tool_loop_max_calls" not in TaskValidationConfig.model_fields
        assert config.prompt_path is None  # Uses default prompt from prompts/
        assert config.close_review_prompt_max_chars == 256_000


class TestWorkflowConfig:
    """Tests for WorkflowConfig."""

    def test_default_values(self) -> None:
        """Test default workflow config."""
        config = WorkflowConfig()
        assert config.enabled is True
        assert config.timeout == 90.0

    def test_timeout_validation(self) -> None:
        """Test the bundled zero sentinel resolves to the default timeout."""
        assert WorkflowConfig(timeout=0).timeout == DEFAULT_WORKFLOW_TIMEOUT_SECONDS


class TestMessageTrackingConfig:
    """Tests for MessageTrackingConfig."""

    def test_default_values(self) -> None:
        """Test default message tracking config."""
        config = MessageTrackingConfig()
        assert config.enabled is True
        assert config.poll_interval == 5.0
        assert config.debounce_delay == 1.0
        assert config.max_message_length == 10000
        assert config.broadcast_enabled is True

    def test_positive_validation(self) -> None:
        """Test positive values validation."""
        with pytest.raises(ValidationError):
            MessageTrackingConfig(poll_interval=0)
        with pytest.raises(ValidationError):
            MessageTrackingConfig(debounce_delay=0)


class TestSessionLifecycleConfig:
    """Tests for SessionLifecycleConfig."""

    def test_default_values(self) -> None:
        """Test default session lifecycle config."""
        config = SessionLifecycleConfig()
        assert config.stale_session_timeout_hours == 24
        assert config.expire_check_interval_minutes == 60
        assert config.transcript_processing_interval_minutes == 5
        assert config.transcript_processing_batch_size == 10
        assert config.workflow_audit_retention_days == 7

    def test_positive_validation(self) -> None:
        """Test positive values validation."""
        with pytest.raises(ValidationError):
            SessionLifecycleConfig(stale_session_timeout_hours=0)
        with pytest.raises(ValidationError):
            SessionLifecycleConfig(workflow_audit_retention_days=0)

    def test_workflow_audit_retention_is_configurable(self) -> None:
        config = SessionLifecycleConfig(workflow_audit_retention_days=21)
        assert config.workflow_audit_retention_days == 21


class TestMemoryConfig:
    """Tests for MemoryConfig."""

    def test_default_values(self) -> None:
        """Test default memory config."""
        config = MemoryConfig()
        assert config.enabled is True
        assert config.backend == "local"
        assert config.crossref_threshold == 0.3
        assert config.access_debounce_seconds == 60
        assert config.kg.profile == FeatureProfile.LOW
        assert "claude/haiku" in candidate_labels(config.kg.candidates)

    def test_crossref_threshold_validation(self) -> None:
        """Test crossref_threshold validation."""
        with pytest.raises(ValidationError):
            MemoryConfig(crossref_threshold=1.5)
        with pytest.raises(ValidationError):
            MemoryConfig(crossref_threshold=-0.1)


# ==============================================================================
# Additional tests for config module decomposition coverage (gt-dfa0d7)
# These tests verify all config classes can be instantiated correctly
# ==============================================================================


class TestToolSummarizerConfig:
    """Tests for ToolSummarizerConfig."""

    def test_default_values(self) -> None:
        """Test default tool summarizer config."""
        config = ToolSummarizerConfig()
        assert config.enabled is True
        assert config.profile == FeatureProfile.LOW
        assert "claude/haiku" in candidate_labels(config.candidates)
        assert config.prompt_path is None  # Uses default prompt from prompts/


class TestGobbyTasksConfig:
    """Tests for GobbyTasksConfig."""

    def test_default_values(self) -> None:
        """Test default gobby tasks config."""
        config = GobbyTasksConfig()
        assert config.enabled is True
        assert config.show_result_on_create is False
        assert isinstance(config.expansion, TaskExpansionConfig)
        assert isinstance(config.validation, TaskValidationConfig)

    def test_nested_configs(self) -> None:
        """Test nested expansion and validation configs."""
        config = GobbyTasksConfig(
            expansion=TaskExpansionConfig(enabled=False),
            validation=TaskValidationConfig(enabled=False),
        )
        assert config.expansion.enabled is False
        assert config.validation.enabled is False


class TestWebhookEndpointConfig:
    """Tests for WebhookEndpointConfig."""

    def test_required_fields(self) -> None:
        """Test required fields."""
        config = WebhookEndpointConfig(name="test", url="https://example.com")
        assert config.name == "test"
        assert config.url == "https://example.com"
        assert config.timeout == 10.0
        assert config.retry_count == 3
        assert config.retry_delay == 1.0
        assert config.can_block is False
        assert config.enabled is True

    def test_custom_values(self) -> None:
        """Test custom webhook config."""
        config = WebhookEndpointConfig(
            name="custom",
            url="https://api.example.com/hook",
            events=["session-start", "session-end"],
            timeout=30.0,
            retry_count=5,
            can_block=True,
        )
        assert len(config.events) == 2
        assert config.timeout == 30.0
        assert config.retry_count == 5
        assert config.can_block is True


class TestWebhooksConfig:
    """Tests for WebhooksConfig."""

    def test_default_values(self) -> None:
        """Test default webhooks config."""
        config = WebhooksConfig()
        assert config.enabled is True
        assert config.endpoints == []
        assert config.default_timeout == 10.0
        assert config.async_dispatch is True

    def test_with_endpoints(self) -> None:
        """Test webhooks config with endpoints."""
        config = WebhooksConfig(
            endpoints=[
                WebhookEndpointConfig(name="test1", url="https://a.com"),
                WebhookEndpointConfig(name="test2", url="https://b.com"),
            ]
        )
        assert len(config.endpoints) == 2


class TestMemoryBackupConfig:
    """Tests for MemoryBackupConfig."""

    def test_default_values(self) -> None:
        """Test default memory backup config."""
        config = MemoryBackupConfig()
        assert config.enabled is True
        assert config.backup_path == Path(".gobby/memories.jsonl")


class TestMetricsConfig:
    """Tests for MetricsConfig."""

    def test_default_values(self) -> None:
        """Test default metrics config."""
        config = MetricsConfig()
        assert config.list_limit == 10000

    def test_list_limit_validation(self) -> None:
        """Test list_limit must be non-negative."""
        config = MetricsConfig(list_limit=0)  # 0 is valid (unbounded)
        assert config.list_limit == 0

        with pytest.raises(ValidationError):
            MetricsConfig(list_limit=-1)


class TestBinFreshnessConfig:
    """Tests for managed native binary freshness config."""

    def test_default_values(self) -> None:
        config = BinFreshnessConfig()
        assert config.enabled is True
        assert config.initial_delay_seconds == 30.0
        assert config.interval_seconds == 3600.0
        assert config.jitter_seconds == 300.0
        assert config.github_timeout_seconds == 30.0

    def test_validation(self) -> None:
        with pytest.raises(ValidationError):
            BinFreshnessConfig(initial_delay_seconds=-1)
        with pytest.raises(ValidationError):
            BinFreshnessConfig(interval_seconds=0)
        with pytest.raises(ValidationError):
            BinFreshnessConfig(jitter_seconds=-1)
        with pytest.raises(ValidationError):
            BinFreshnessConfig(github_timeout_seconds=0)


class TestGenerationConfig:
    """Tests for daemon text generation config."""

    def test_candidate_timeout_default(self) -> None:
        config = GenerationConfig()
        assert config.timeout_seconds == 1200.0
        assert config.candidate_timeout_seconds == 30.0
        assert config.cli_candidate_timeout_seconds == 600.0
        assert config.spawn_cold_max_concurrency == 3

    def test_candidate_timeout_validation(self) -> None:
        with pytest.raises(ValidationError):
            GenerationConfig(candidate_timeout_seconds=0)
        with pytest.raises(ValidationError):
            GenerationConfig(candidate_timeout_seconds=-1)

    def test_spawn_cold_max_concurrency_override(self) -> None:
        config = DaemonConfig(ai={"generation": {"spawn_cold_max_concurrency": 4}})

        assert config.ai.generation.spawn_cold_max_concurrency == 4

    def test_spawn_cold_max_concurrency_validation(self) -> None:
        with pytest.raises(ValidationError):
            GenerationConfig(spawn_cold_max_concurrency=0)
        with pytest.raises(ValidationError):
            GenerationConfig(spawn_cold_max_concurrency=-1)


# ==============================================================================
# Cross-module reference tests (ensure DaemonConfig wires everything correctly)
# ==============================================================================


@pytest.mark.no_config_protection
class TestDaemonConfigComposition:
    """Tests for DaemonConfig composition with sub-configs."""

    def test_all_sub_configs_accessible(self) -> None:
        """Test all sub-configs are accessible from DaemonConfig."""
        config = DaemonConfig()

        # Network/server
        assert isinstance(config.websocket, WebSocketSettings)
        assert isinstance(config.telemetry, TelemetrySettings)

        # Session
        assert isinstance(config.session_summary, SessionSummaryConfig)
        assert isinstance(config.session_lifecycle, SessionLifecycleConfig)
        assert isinstance(config.message_tracking, MessageTrackingConfig)

        # MCP
        assert isinstance(config.mcp_client_proxy, MCPClientProxyConfig)
        assert isinstance(config.import_mcp_server, ImportMCPServerConfig)
        assert isinstance(config.tool_summarizer, ToolSummarizerConfig)

        # Tasks
        assert isinstance(config.gobby_tasks, GobbyTasksConfig)
        assert isinstance(config.gobby_tasks.expansion, TaskExpansionConfig)
        assert isinstance(config.gobby_tasks.validation, TaskValidationConfig)

        # LLM
        assert "llm_providers" not in DaemonConfig.model_fields
        assert not hasattr(config, "llm_providers")
        assert isinstance(config.recommend_tools, RecommendToolsConfig)

        # Hooks
        assert isinstance(config.hooks, HookTimeoutConfig)
        assert isinstance(config.hook_extensions, HookExtensionsConfig)
        assert isinstance(config.hook_extensions.websocket, WebSocketBroadcastConfig)
        assert isinstance(config.hook_extensions.webhooks, WebhooksConfig)

        # Workflow
        assert isinstance(config.workflow, WorkflowConfig)
        assert isinstance(config.metrics, MetricsConfig)
        assert isinstance(config.bin_freshness, BinFreshnessConfig)
        assert isinstance(config.verification_defaults, ProjectVerificationConfig)
        assert isinstance(config.project_verification_synthesis, ProjectVerificationSynthesisConfig)

        # Memory
        assert isinstance(config.memory, MemoryConfig)
        assert isinstance(config.memory_backup, MemoryBackupConfig)

    def test_getters_return_correct_configs(self) -> None:
        """Test all getter methods return correct configs."""
        config = DaemonConfig()

        assert config.get_tool_result_offload_config() is config.tool_result_offload
        assert config.get_import_mcp_server_config() is config.import_mcp_server
        assert config.get_gobby_tasks_config() is config.gobby_tasks
        assert config.get_search_config() is config.search

    def test_yaml_round_trip(self, temp_dir: Path) -> None:
        """Test config survives YAML export and reimport."""
        config = DaemonConfig(
            daemon_port=9000,
            logging={"level": "debug"},
            memory=MemoryConfig(crossref_threshold=0.8),
        )

        # Export to YAML
        config_file = temp_dir / "roundtrip.yaml"
        export_config_to_yaml(config, str(config_file))

        # Verify YAML content is valid and preserves values
        raw = yaml.safe_load(config_file.read_text())
        assert raw["daemon_port"] == 9000
        assert raw["logging"]["level"] == "debug"
        assert raw["memory"]["crossref_threshold"] == 0.8

        # Verify it can be loaded back into DaemonConfig
        loaded = DaemonConfig(**raw)
        assert loaded.daemon_port == 9000
        assert loaded.logging.level == "debug"
        assert loaded.memory.crossref_threshold == 0.8


class TestChatConfig:
    def test_attachment_total_default_matches_product_cap(self) -> None:
        """Default total attachment cap matches file cap times file count."""
        config = ChatConfig()

        assert config.attachment_max_total_bytes_per_message == (
            config.attachment_max_file_bytes * config.attachment_max_files_per_message
        )

    def test_attachment_total_rejects_values_above_product_cap(self) -> None:
        """Configured total attachment cap cannot exceed the product cap."""
        with pytest.raises(ValidationError, match="attachment_max_total_bytes_per_message"):
            ChatConfig(
                attachment_max_file_bytes=10,
                attachment_max_files_per_message=2,
                attachment_max_total_bytes_per_message=21,
            )


class TestAllConfigClassesInstantiate:
    """Verify all active config classes can be instantiated with defaults."""

    def test_all_classes_instantiate(self) -> None:
        """Test all config classes instantiate without error."""
        # This test ensures the baseline works before extraction
        configs = [
            WebSocketSettings(),
            TelemetrySettings(),
            SessionSummaryConfig(),
            ToolSummarizerConfig(),
            RecommendToolsConfig(),
            ImportMCPServerConfig(),
            ProjectVerificationConfig(),
            ProjectVerificationSynthesisConfig(),
            MCPClientProxyConfig(),
            GobbyTasksConfig(),
            WebSocketBroadcastConfig(),
            WebhookEndpointConfig(name="test", url="https://test.com"),  # Required
            WebhooksConfig(),
            HookExtensionsConfig(),
            TaskExpansionConfig(),
            TaskValidationConfig(),
            WorkflowConfig(),
            MessageTrackingConfig(),
            SessionLifecycleConfig(),
            MetricsConfig(),
            BinFreshnessConfig(),
            MemoryConfig(),
            MemoryBackupConfig(),
            DaemonConfig(),
        ]

        assert len(configs) == 24
        for config in configs:
            assert config is not None
