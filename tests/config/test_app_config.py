"""Tests for the configuration system."""

import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml
from pydantic import ValidationError

from gobby.config.app import (
    DaemonConfig,
    apply_cli_overrides,
    expand_env_vars,
    load_config,
    load_yaml,
    save_config,
)
from gobby.config.bin_freshness import BinFreshnessConfig
from gobby.config.embedding_keys import (
    AI_EMBEDDING_API_BASE_KEY,
    AI_EMBEDDING_DIM_KEY,
    AI_EMBEDDING_MODEL_KEY,
    runtime_embedding_key,
)
from gobby.config.extensions import (
    HookExtensionsConfig,
    WebhookEndpointConfig,
    WebhooksConfig,
    WebSocketBroadcastConfig,
)
from gobby.config.features import (
    ChatConfig,
    ImportMCPServerConfig,
    MetricsConfig,
    RecommendToolsConfig,
    ToolSummarizerConfig,
)
from gobby.config.llm_providers import LLMProviderConfig, LLMProvidersConfig
from gobby.config.persistence import MemoryBackupConfig, MemoryConfig
from gobby.config.servers import MCPClientProxyConfig, WebSocketSettings
from gobby.config.sessions import (
    ContextInjectionConfig,
    DigestConfig,
    MessageTrackingConfig,
    SessionLifecycleConfig,
    SessionSummaryConfig,
)
from gobby.config.tasks import (
    CompactHandoffConfig,
    GobbyTasksConfig,
    TaskExpansionConfig,
    TaskValidationConfig,
    WorkflowConfig,
)
from gobby.config.ui import UIConfig
from gobby.telemetry.config import TelemetrySettings

pytestmark = pytest.mark.unit


def write_secure_bootstrap(path: Path, content: str) -> None:
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
            with patch("gobby.config.app.logger") as mock_logger:
                result = expand_env_vars("key: ${MISSING_VAR}", secret_resolver=resolver)
                assert result == "key: ${MISSING_VAR}"
                mock_logger.warning.assert_called_once()
                assert "MISSING_VAR" in mock_logger.warning.call_args[0][0]

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
            with patch("gobby.config.app.logger") as mock_logger:
                result = expand_env_vars("key: ${NOPE}")
                assert result == "key: ${NOPE}"
                mock_logger.warning.assert_called_once()

    def test_secret_ref_resolved(self) -> None:
        """Test $secret:NAME resolves from secrets store only."""

        def resolver(name: str) -> str | None:
            return "my_secret" if name == "API_KEY" else None

        result = expand_env_vars("key: $secret:API_KEY", secret_resolver=resolver)
        assert result == "key: my_secret"

    def test_secret_ref_no_env_fallback(self) -> None:
        """Test $secret:NAME does NOT fall back to env vars."""

        def resolver(name: str) -> str | None:
            return None

        with patch.dict(os.environ, {"API_KEY": "env_value"}):
            with patch("gobby.config.app.logger"):
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

        with patch("gobby.config.app.logger") as mock_logger:
            result = expand_env_vars("key: $secret:MISSING", secret_resolver=resolver)
            assert result == "key: $secret:MISSING"
            mock_logger.warning.assert_called_once()
            assert "MISSING" in mock_logger.warning.call_args[0][0]

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
        assert config.provider == "claude"
        assert config.model == "sonnet"
        # prompt now has a default template with placeholders
        assert config.prompt is not None
        assert "Generate a concise session summary" in config.prompt

    def test_custom_values(self) -> None:
        """Test custom session summary config."""
        config = SessionSummaryConfig(
            enabled=False,
            provider="gemini",
            model="gemini-2.0-flash",
            prompt="Custom prompt",
        )
        assert config.enabled is False
        assert config.provider == "gemini"
        assert config.model == "gemini-2.0-flash"
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


class TestLLMProviderConfig:
    """Tests for LLMProviderConfig."""

    def test_models_list(self) -> None:
        """Test getting models as list."""
        config = LLMProviderConfig(
            models="model-a, model-b, model-c",
        )
        models = config.get_models_list()
        assert models == ["model-a", "model-b", "model-c"]

    def test_empty_models_in_list(self) -> None:
        """Test empty model entries are filtered."""
        config = LLMProviderConfig(
            models="model-a, , model-b",
        )
        models = config.get_models_list()
        assert models == ["model-a", "model-b"]


class TestLLMProvidersConfig:
    """Tests for LLMProvidersConfig."""

    def test_default_has_claude(self) -> None:
        """Test default config has Claude provider enabled."""
        config = LLMProvidersConfig()
        assert config.get_enabled_providers() == ["claude"]

    def test_enabled_providers(self) -> None:
        """Test listing LLMProvider-backed generation bindings."""
        config = LLMProvidersConfig(
            claude=LLMProviderConfig(models="claude-haiku-4-5"),
        )
        providers = config.get_enabled_providers()
        assert providers == ["claude"]

    def test_removed_cli_provider_configs_are_ignored(self) -> None:
        """CLI/app-server providers do not become LLMProvider config entries."""
        config = LLMProvidersConfig(
            codex=LLMProviderConfig(models="removed"),
            gemini=LLMProviderConfig(models="removed"),
            grok=LLMProviderConfig(models="removed"),
            qwen=LLMProviderConfig(models="removed"),
        )

        assert config.get_enabled_providers() == ["claude"]
        assert not hasattr(config, "codex")

    def test_unknown_provider_configs_are_rejected(self) -> None:
        """Unknown provider keys still fail validation."""
        with pytest.raises(ValidationError):
            LLMProvidersConfig(openai=LLMProviderConfig(models="removed"))


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

    def test_sub_config_access(self) -> None:
        """Test accessing sub-configurations."""
        config = DaemonConfig()
        assert config.get_recommend_tools_config() == config.recommend_tools
        assert config.get_mcp_client_proxy_config() == config.mcp_client_proxy

    def test_get_verification_defaults(self) -> None:
        """Test get_verification_defaults returns verification_defaults config."""
        config = DaemonConfig()
        verification_config = config.get_verification_defaults()
        assert verification_config is config.verification_defaults
        # Verify it returns the correct type
        from gobby.config.features import ProjectVerificationConfig

        assert isinstance(verification_config, ProjectVerificationConfig)

    def test_rejects_removed_conductor_section(self) -> None:
        """Stale top-level conductor config should fail loudly."""
        with pytest.raises(ValidationError, match="conductor config has been removed"):
            DaemonConfig(conductor={"enabled": False})


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

    def test_empty_json_file(self, temp_dir: Path) -> None:
        """Test loading empty JSON file returns empty dict."""
        config_file = temp_dir / "empty.json"
        config_file.write_text("")

        data = load_yaml(str(config_file))
        assert data == {}

    def test_env_var_expansion_in_yaml(self, temp_dir: Path, monkeypatch) -> None:
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
        config = {}
        overrides = {"logging.level": "debug"}

        result = apply_cli_overrides(config, overrides)
        assert result["logging"]["level"] == "debug"

    def test_none_overrides(self) -> None:
        """Test None overrides returns config unchanged."""
        config = {"key": "value"}
        result = apply_cli_overrides(config, None)
        assert result == config


@pytest.mark.no_config_protection
class TestLoadConfig:
    """Tests for load_config function."""

    def test_load_default_config(self, temp_dir: Path, monkeypatch) -> None:
        """Test loading default config when no file exists."""
        monkeypatch.chdir(temp_dir)
        config = load_config(config_file=str(temp_dir / "nonexistent.yaml"))
        assert isinstance(config, DaemonConfig)

    def test_load_with_yaml_file(self, temp_dir: Path) -> None:
        """Test loading config from YAML file."""
        config_file = temp_dir / "config.yaml"
        config_file.write_text(yaml.dump({"daemon_port": 9000}))

        config = load_config(config_file=str(config_file))
        assert config.daemon_port == 9000

    def test_load_with_cli_overrides(self, temp_dir: Path) -> None:
        """Test loading config with CLI overrides."""
        config_file = temp_dir / "config.yaml"
        config_file.write_text(yaml.dump({"daemon_port": 8000}))

        config = load_config(
            config_file=str(config_file),
            cli_overrides={"daemon_port": 9000},
        )
        assert config.daemon_port == 9000

    def test_missing_config_file_uses_defaults(self, temp_dir: Path) -> None:
        """Test that a missing config file falls back to Pydantic defaults."""
        config_file = temp_dir / "new_config.yaml"
        assert not config_file.exists()

        config = load_config(config_file=str(config_file))
        assert config.daemon_port == 60887  # Pydantic default

    def test_load_config_with_none_path_uses_bootstrap(self, temp_dir: Path, monkeypatch) -> None:
        """Test loading config with config_file=None reads bootstrap.yaml."""
        default_path = temp_dir / ".gobby" / "bootstrap.yaml"
        default_path.parent.mkdir(parents=True, exist_ok=True)
        write_secure_bootstrap(default_path, yaml.dump({"daemon_port": 7777}))
        monkeypatch.setenv("GOBBY_HOME", str(default_path.parent))

        config = load_config(config_file=None)
        assert config.daemon_port == 7777

    def test_load_config_validation_error(self, temp_dir: Path) -> None:
        """Test load_config raises ValueError on invalid bootstrap configuration."""
        bootstrap_file = temp_dir / "bootstrap.yaml"
        # Write invalid port value (out of range)
        write_secure_bootstrap(bootstrap_file, yaml.dump({"daemon_port": 80}))

        with pytest.raises(ValueError, match="Configuration validation failed"):
            load_config(config_file=str(bootstrap_file))

    def test_load_config_invalid_type_falls_back_to_defaults(self, temp_dir: Path) -> None:
        """Test load_config falls back to defaults when bootstrap has invalid type."""
        bootstrap_file = temp_dir / "bootstrap.yaml"
        # Write string instead of int for port — bootstrap silently falls back
        write_secure_bootstrap(bootstrap_file, "daemon_port: not_a_number")

        config = load_config(config_file=str(bootstrap_file))
        # Bootstrap swallows the int() conversion error and returns defaults
        assert config.daemon_port == 60887

    def test_load_config_rejects_legacy_session_title_db_keys(self, temp_dir: Path) -> None:
        """Legacy session_title DB config now fails loudly instead of being migrated."""

        class DummyConfigStore:
            def get_all(self) -> dict[str, object]:
                return {
                    "session_title.provider": "local",
                    "session_title.model": "gemma-3",
                    "session_title.timeout": 45,
                }

        with pytest.raises(ValueError, match="session_title config has been removed"):
            load_config(
                config_file=str(temp_dir / "bootstrap.yaml"),
                config_store=DummyConfigStore(),
            )

    def test_load_config_rejects_legacy_session_title_even_with_explicit_digest(
        self, temp_dir: Path
    ) -> None:
        """Explicit digest config does not mask stale legacy session_title DB keys."""

        class DummyConfigStore:
            def get_all(self) -> dict[str, object]:
                return {
                    "session_title.provider": "local",
                    "session_title.model": "gemma-3",
                    "session_title.timeout": 45,
                    "digest.provider": "claude",
                    "digest.model": "haiku",
                    "digest.timeout": 15,
                }

        with pytest.raises(ValueError, match="session_title config has been removed"):
            load_config(
                config_file=str(temp_dir / "bootstrap.yaml"),
                config_store=DummyConfigStore(),
            )

    def test_load_config_migrates_memory_kg_keys_from_db(self, temp_dir: Path) -> None:
        """Legacy memory.kg_provider/kg_model DB keys migrate to memory.kg.*."""

        class DummyConfigStore:
            def get_all(self) -> dict[str, object]:
                return {
                    "memory.kg_provider": "codex",
                    "memory.kg_model": "gpt-5-mini",
                }

        config = load_config(
            config_file=str(temp_dir / "bootstrap.yaml"),
            config_store=DummyConfigStore(),
        )

        assert config.memory.kg.provider == "codex"
        assert config.memory.kg.model == "gpt-5-mini"

    def test_ai_embeddings_normalized_at_load(self, temp_dir: Path) -> None:
        """Canonical DB embedding keys populate the runtime embeddings model."""

        class DummyConfigStore:
            def get_all(self) -> dict[str, object]:
                return {
                    AI_EMBEDDING_MODEL_KEY: "bge-m3",
                    AI_EMBEDDING_API_BASE_KEY: "http://localhost:9999/v1",
                    AI_EMBEDDING_DIM_KEY: 1024,
                }

        config = load_config(
            config_file=str(temp_dir / "bootstrap.yaml"),
            config_store=DummyConfigStore(),
        )

        assert config.embeddings.model == "bge-m3"
        assert config.embeddings.api_base == "http://localhost:9999/v1"
        assert config.embeddings.dim == 1024

    def test_legacy_embedding_db_keys_are_ignored(self, temp_dir: Path) -> None:
        """Old DB embedding keys are no longer honored after the hard cut."""

        class DummyConfigStore:
            def __init__(self) -> None:
                self.deleted: list[str] = []

            def get_all(self) -> dict[str, object]:
                return {
                    runtime_embedding_key("model"): "legacy-model",
                    runtime_embedding_key("api_base"): "http://legacy/v1",
                    runtime_embedding_key("provider"): "ollama",
                    AI_EMBEDDING_MODEL_KEY: "canonical-model",
                    AI_EMBEDDING_DIM_KEY: 768,
                    "ai.embeddings.provider": "lmstudio",
                }

            def delete(self, key: str) -> bool:
                self.deleted.append(key)
                return True

        store = DummyConfigStore()

        config = load_config(
            config_file=str(temp_dir / "bootstrap.yaml"),
            config_store=store,
        )

        assert config.embeddings.model == "canonical-model"
        assert config.embeddings.api_base is None
        assert sorted(store.deleted) == [
            "ai.embeddings.provider",
            runtime_embedding_key("api_base"),
            runtime_embedding_key("model"),
            runtime_embedding_key("provider"),
        ]

    def test_load_config_drops_stale_neo4j_db_keys(
        self, temp_dir: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Legacy databases.neo4j.* config_store keys do not block FalkorDB config."""

        class DummyConfigStore:
            def __init__(self) -> None:
                self.deleted: list[str] = []

            def get_all(self) -> dict[str, object]:
                return {
                    "databases.neo4j.url": "http://localhost:8474",
                    "databases.neo4j.password": "$secret:password",
                    "databases.falkordb.requirepass": "falkor-secret",
                }

            def delete(self, key: str) -> bool:
                self.deleted.append(key)
                return True

        store = DummyConfigStore()
        caplog.set_level("WARNING", logger="gobby.config.app")

        config = load_config(
            config_file=str(temp_dir / "bootstrap.yaml"),
            config_store=store,
        )

        assert config.databases.falkordb.requirepass == "falkor-secret"
        assert store.deleted == ["databases.neo4j.password", "databases.neo4j.url"]
        assert any(
            "Ignoring stale Neo4j config_store keys after FalkorDB migration" in record.getMessage()
            for record in caplog.records
        )

    def test_load_config_migrates_defaults_seeded_ui_mode_to_auto(self, temp_dir: Path) -> None:
        """Only defaults-sourced legacy ui.mode=production rows migrate to auto."""

        class DummyCursor:
            rowcount = 1

        class DummyDB:
            def __init__(self) -> None:
                self.calls: list[tuple[str, tuple[object, ...]]] = []

            def execute(self, query: str, params: tuple[object, ...]) -> DummyCursor:
                self.calls.append((query, params))
                return DummyCursor()

        class DummyConfigStore:
            def __init__(self) -> None:
                self.db = DummyDB()

            def get_all(self) -> dict[str, object]:
                return {"ui.mode": "production"}

        store = DummyConfigStore()

        config = load_config(
            config_file=str(temp_dir / "bootstrap.yaml"),
            config_store=store,
        )

        assert config.ui.mode == "auto"
        assert store.db.calls
        assert store.db.calls[0][1][2:] == ("ui.mode", "defaults", '"production"')

    def test_load_config_leaves_explicit_ui_mode_production_untouched(self, temp_dir: Path) -> None:
        """Non-defaults ui.mode=production rows remain explicit production."""

        class DummyCursor:
            rowcount = 0

        class DummyDB:
            def execute(self, _query: str, _params: tuple[object, ...]) -> DummyCursor:
                return DummyCursor()

        class DummyConfigStore:
            db = DummyDB()

            def get_all(self) -> dict[str, object]:
                return {"ui.mode": "production"}

        config = load_config(
            config_file=str(temp_dir / "bootstrap.yaml"),
            config_store=DummyConfigStore(),
        )

        assert config.ui.mode == "production"

    def test_load_config_drops_removed_dead_sections(self, temp_dir: Path) -> None:
        """Removed review/task_description/enrichment sections are ignored from DB config."""

        class DummyConfigStore:
            def get_all(self) -> dict[str, object]:
                return {
                    "review.model": "opus",
                    "task_description.enabled": False,
                    "gobby-tasks.enrichment.enabled": False,
                }

        config = load_config(
            config_file=str(temp_dir / "bootstrap.yaml"),
            config_store=DummyConfigStore(),
        )

        assert not hasattr(config, "review")
        assert not hasattr(config, "task_description")
        assert not hasattr(config.gobby_tasks, "enrichment")

    def test_load_config_clamps_legacy_cron_interval_from_db(self, temp_dir: Path) -> None:
        """Legacy cron intervals below the scheduler floor do not block startup."""

        class DummyConfigStore:
            def get_all(self) -> dict[str, object]:
                return {"cron.check_interval_seconds": 30}

        config = load_config(
            config_file=str(temp_dir / "bootstrap.yaml"),
            config_store=DummyConfigStore(),
        )

        assert config.cron.check_interval_seconds == 60

    @pytest.mark.parametrize("stale_mode", ["native", "external"])
    def test_load_config_normalizes_stale_postgres_install_modes_from_config_file(
        self,
        temp_dir: Path,
        stale_mode: str,
    ) -> None:
        """Legacy config file install modes do not block startup after Docker-only migration."""

        config_file = temp_dir / "config.yaml"
        write_secure_bootstrap(config_file, f"postgres_install_mode: {stale_mode}\n")

        class DummyConfigStore:
            def get_all(self) -> dict[str, object]:
                return {}

        config = load_config(
            config_file=str(config_file),
            config_store=DummyConfigStore(),
        )

        assert config.postgres_install_mode == "docker"

    def test_load_config_preserves_bootstrap_backend_selection_over_db(
        self, temp_dir: Path
    ) -> None:
        """DB config cannot override bootstrap-level hub backend selection."""

        bootstrap_file = temp_dir / "bootstrap.yaml"
        write_secure_bootstrap(
            bootstrap_file,
            "hub_backend: postgres\n"
            "database_url: postgresql://gobby:secret@localhost:60891/gobby\n"
            "postgres_install_mode: docker\n",
        )

        class DummyConfigStore:
            def get_all(self) -> dict[str, object]:
                return {
                    "hub_backend": "local",
                    "database_url": None,
                    "postgres_install_mode": "external",
                }

        config = load_config(
            config_file=str(bootstrap_file),
            config_store=DummyConfigStore(),
            resolve_database_url=True,
        )

        assert config.hub_backend == "postgres"
        assert config.database_url == "postgresql://gobby:secret@localhost:60891/gobby"
        assert config.postgres_install_mode == "docker"

    def test_load_config_without_resolution_reads_plaintext_dsn(self, temp_dir: Path) -> None:
        """Config readers can inspect bootstrap fields without special credential access."""

        bootstrap_file = temp_dir / "bootstrap.yaml"
        write_secure_bootstrap(
            bootstrap_file,
            "hub_backend: postgres\n"
            "database_url: postgresql://gobby:secret@localhost:60891/gobby\n"
            "daemon_port: 61234\n",
        )

        config = load_config(config_file=str(bootstrap_file))

        assert config.daemon_port == 61234
        assert config.database_url == "postgresql://gobby:secret@localhost:60891/gobby"


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

        assert bootstrap.falkordb_password == "gobbyfalkor"

    def test_to_config_dict(self) -> None:
        """Test bootstrap converts to DaemonConfig-compatible dict."""
        from gobby.config.bootstrap import BootstrapConfig

        bootstrap = BootstrapConfig(daemon_port=7777, websocket_port=7778)
        d = bootstrap.to_config_dict()
        assert d["daemon_port"] == 7777
        assert d["websocket"]["port"] == 7778
        assert d["bind_host"] == "localhost"
        assert d["hub_backend"] == "postgres"
        assert d["database_url"] is None
        assert d["postgres_install_mode"] is None

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
    """Tests for save_config function."""

    def test_saves_config(self, temp_dir: Path, default_config: DaemonConfig) -> None:
        """Test saving config to file."""
        config_file = temp_dir / "saved.yaml"
        save_config(default_config, str(config_file))

        assert config_file.exists()
        content = yaml.safe_load(config_file.read_text())
        assert content["daemon_port"] == default_config.daemon_port

    def test_file_permissions(self, temp_dir: Path, default_config: DaemonConfig) -> None:
        """Test saved config has restrictive permissions."""
        config_file = temp_dir / "secure.yaml"
        save_config(default_config, str(config_file))

        # Check permissions (0o600 = owner read/write only)
        mode = config_file.stat().st_mode & 0o777
        assert mode == 0o600

    def test_creates_parent_directory(self, temp_dir: Path, default_config: DaemonConfig) -> None:
        """Test creating parent directory when saving."""
        config_file = temp_dir / "nested" / "dir" / "config.yaml"
        save_config(default_config, str(config_file))

        assert config_file.exists()

    def test_save_config_with_none_path_uses_default(
        self, temp_dir: Path, default_config: DaemonConfig, monkeypatch
    ) -> None:
        """Test saving config with config_file=None uses default path."""
        monkeypatch.delenv("GOBBY_TEST_PROTECT", raising=False)

        # Patch expanduser to redirect ~/.gobby to temp_dir/.gobby
        original_expanduser = Path.expanduser

        def mock_expanduser(self):
            path_str = str(self)
            if path_str.startswith("~/.gobby"):
                return temp_dir / ".gobby" / path_str[9:]  # Remove ~/.gobby/
            return original_expanduser(self)

        monkeypatch.setattr(Path, "expanduser", mock_expanduser)

        save_config(default_config, config_file=None)

        # Check the file was saved to the mocked default path
        expected_path = temp_dir / ".gobby" / "config.yaml"
        assert expected_path.exists()


@pytest.mark.no_config_protection
class TestSaveConfigTestGuard:
    """Tests for save_config GOBBY_TEST_PROTECT guard."""

    def test_raises_when_test_protect_set_and_no_path(
        self, default_config: DaemonConfig, monkeypatch
    ) -> None:
        """save_config raises RuntimeError when GOBBY_TEST_PROTECT=1 and config_file is None."""
        monkeypatch.setenv("GOBBY_TEST_PROTECT", "1")

        with pytest.raises(
            RuntimeError,
            match="export_config_to_yaml.*would write to production path.*during tests",
        ):
            save_config(default_config, config_file=None)

    def test_no_error_with_explicit_path(
        self, temp_dir: Path, default_config: DaemonConfig, monkeypatch
    ) -> None:
        """save_config works with explicit path even when GOBBY_TEST_PROTECT=1."""
        monkeypatch.setenv("GOBBY_TEST_PROTECT", "1")

        config_file = temp_dir / "safe.yaml"
        save_config(default_config, str(config_file))
        assert config_file.exists()

    def test_no_error_without_test_protect(
        self, temp_dir: Path, default_config: DaemonConfig, monkeypatch
    ) -> None:
        """save_config works normally when GOBBY_TEST_PROTECT is not set."""
        monkeypatch.delenv("GOBBY_TEST_PROTECT", raising=False)

        # Redirect expanduser so we don't touch real config
        original_expanduser = Path.expanduser

        def mock_expanduser(self):
            path_str = str(self)
            if path_str.startswith("~/.gobby"):
                return temp_dir / ".gobby" / path_str[9:]
            return original_expanduser(self)

        monkeypatch.setattr(Path, "expanduser", mock_expanduser)

        save_config(default_config, config_file=None)
        assert (temp_dir / ".gobby" / "config.yaml").exists()


class TestRecommendToolsConfig:
    """Tests for RecommendToolsConfig."""

    def test_default_values(self) -> None:
        """Test default recommend tools config."""
        config = RecommendToolsConfig()
        assert config.enabled is True
        assert config.provider == "claude"
        assert config.model == "sonnet"
        assert config.prompt_path is None  # Uses default prompt from prompts/


class TestImportMCPServerConfig:
    """Tests for ImportMCPServerConfig."""

    def test_default_values(self) -> None:
        """Test default import MCP server config."""
        config = ImportMCPServerConfig()
        assert config.enabled is True
        assert config.provider == "claude"
        assert config.model == "haiku"
        assert config.prompt_path is None  # Uses DEFAULT_IMPORT_MCP_SERVER_PROMPT


class TestDigestConfig:
    """Tests for DigestConfig."""

    def test_default_values(self) -> None:
        """Test default digest config."""
        config = DigestConfig()
        assert config.enabled is True
        assert config.provider == "claude"
        assert config.model == "haiku"
        assert config.timeout == 30

    def test_timeout_validation(self) -> None:
        """Test digest timeout must be positive."""
        with pytest.raises(ValidationError):
            DigestConfig(timeout=0)

    def test_rejects_removed_session_title_section(self) -> None:
        """Legacy session_title config is a hard validation failure."""
        with pytest.raises(ValidationError, match="session_title config has been removed"):
            DaemonConfig(session_title={"provider": "local"})


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
        assert config.provider == "claude"
        assert config.model == "opus"  # Uses opus for complex task expansion
        assert config.tier == "high"
        assert config.prompt_path is None  # Uses default prompt from prompts/


class TestTaskValidationConfig:
    """Tests for TaskValidationConfig."""

    def test_default_values(self) -> None:
        """Test default task validation config."""
        config = TaskValidationConfig()
        assert config.enabled is True
        assert config.provider == "claude"
        assert config.model == "sonnet"
        assert config.tier == "mid"
        assert config.prompt_path is None  # Uses default prompt from prompts/


class TestWorkflowConfig:
    """Tests for WorkflowConfig."""

    def test_default_values(self) -> None:
        """Test default workflow config."""
        config = WorkflowConfig()
        assert config.enabled is True
        assert config.timeout == 0.0

    def test_timeout_validation(self) -> None:
        """Test timeout must be positive."""
        with pytest.raises(ValidationError):
            WorkflowConfig(timeout=-1)


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

    def test_positive_validation(self) -> None:
        """Test positive values validation."""
        with pytest.raises(ValidationError):
            SessionLifecycleConfig(stale_session_timeout_hours=0)


class TestMemoryConfig:
    """Tests for MemoryConfig."""

    def test_default_values(self) -> None:
        """Test default memory config."""
        config = MemoryConfig()
        assert config.enabled is True
        assert config.backend == "local"
        assert config.crossref_threshold == 0.3
        assert config.access_debounce_seconds == 60
        assert config.kg.provider == "claude"
        assert config.kg.model == "haiku"

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


class TestCompactHandoffConfig:
    """Tests for CompactHandoffConfig."""

    def test_default_values(self) -> None:
        """Test default compact handoff config."""
        config = CompactHandoffConfig()
        assert config.enabled is True
        assert config.refresh_timeout_seconds == 180.0

    def test_custom_values(self) -> None:
        """Test custom compact handoff config."""
        config = CompactHandoffConfig(enabled=False, refresh_timeout_seconds=45.0)
        assert config.enabled is False
        assert config.refresh_timeout_seconds == 45.0


class TestContextInjectionConfig:
    """Tests for ContextInjectionConfig."""

    def test_default_values(self) -> None:
        """Test default context injection config."""
        config = ContextInjectionConfig()
        assert config.enabled is True
        assert config.default_source == "summary_markdown"
        assert config.max_file_size == 51200
        assert config.max_content_size == 51200
        assert config.max_transcript_messages == 100

    def test_positive_validation(self) -> None:
        """Test positive value validation."""
        with pytest.raises(ValidationError):
            ContextInjectionConfig(max_file_size=0)
        with pytest.raises(ValidationError):
            ContextInjectionConfig(max_content_size=-1)
        with pytest.raises(ValidationError):
            ContextInjectionConfig(max_transcript_messages=0)


class TestToolSummarizerConfig:
    """Tests for ToolSummarizerConfig."""

    def test_default_values(self) -> None:
        """Test default tool summarizer config."""
        config = ToolSummarizerConfig()
        assert config.enabled is True
        assert config.provider == "claude"
        assert config.model == "haiku"
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
        assert config.export_debounce == 5.0

    def test_debounce_validation(self) -> None:
        """Test export debounce validation."""
        with pytest.raises(ValidationError):
            MemoryBackupConfig(export_debounce=-1.0)


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
        assert isinstance(config.compact_handoff, CompactHandoffConfig)
        assert isinstance(config.context_injection, ContextInjectionConfig)
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
        assert isinstance(config.llm_providers, LLMProvidersConfig)
        assert isinstance(config.digest, DigestConfig)
        assert isinstance(config.recommend_tools, RecommendToolsConfig)

        # Hooks
        assert isinstance(config.hook_extensions, HookExtensionsConfig)
        assert isinstance(config.hook_extensions.websocket, WebSocketBroadcastConfig)
        assert isinstance(config.hook_extensions.webhooks, WebhooksConfig)

        # Workflow
        assert isinstance(config.workflow, WorkflowConfig)
        assert isinstance(config.metrics, MetricsConfig)
        assert isinstance(config.bin_freshness, BinFreshnessConfig)

        # Memory
        assert isinstance(config.memory, MemoryConfig)
        assert isinstance(config.memory_sync, MemoryBackupConfig)

    def test_getters_return_correct_configs(self) -> None:
        """Test all getter methods return correct configs."""
        config = DaemonConfig()

        assert config.get_recommend_tools_config() is config.recommend_tools
        assert config.get_tool_summarizer_config() is config.tool_summarizer
        assert config.get_import_mcp_server_config() is config.import_mcp_server
        assert config.get_mcp_client_proxy_config() is config.mcp_client_proxy
        assert config.get_memory_config() is config.memory
        assert config.get_memory_sync_config() is config.memory_sync
        assert config.get_gobby_tasks_config() is config.gobby_tasks
        assert config.get_metrics_config() is config.metrics

    def test_yaml_round_trip(self, temp_dir: Path) -> None:
        """Test config survives YAML export and reimport."""
        config = DaemonConfig(
            daemon_port=9000,
            telemetry=TelemetrySettings(log_level="debug"),
            memory=MemoryConfig(crossref_threshold=0.8),
        )

        # Export to YAML
        config_file = temp_dir / "roundtrip.yaml"
        save_config(config, str(config_file))

        # Verify YAML content is valid and preserves values
        raw = yaml.safe_load(config_file.read_text())
        assert raw["daemon_port"] == 9000
        assert raw["telemetry"]["log_level"] == "debug"
        assert raw["memory"]["crossref_threshold"] == 0.8

        # Verify it can be loaded back into DaemonConfig
        loaded = DaemonConfig(**raw)
        assert loaded.daemon_port == 9000
        assert loaded.telemetry.log_level == "debug"
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
    """Verify all 30 config classes can be instantiated with defaults."""

    def test_all_classes_instantiate(self) -> None:
        """Test all config classes instantiate without error."""
        # This test ensures the baseline works before extraction
        configs = [
            WebSocketSettings(),
            TelemetrySettings(),
            CompactHandoffConfig(),
            ContextInjectionConfig(),
            SessionSummaryConfig(),
            ToolSummarizerConfig(),
            RecommendToolsConfig(),
            ImportMCPServerConfig(),
            MCPClientProxyConfig(),
            GobbyTasksConfig(),
            LLMProviderConfig(models="test-model"),  # Required field
            LLMProvidersConfig(),
            DigestConfig(),
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

        assert len(configs) == 27
        for config in configs:
            assert config is not None
