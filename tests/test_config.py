"""Tests for the configuration system."""

import json
from pathlib import Path

import pytest
import yaml
from gobby.config.app import (
    CodeExecutionConfig,
    DaemonConfig,
    LLMProviderConfig,
    LLMProvidersConfig,
    LoggingSettings,
    MCPClientProxyConfig,
    SessionSummaryConfig,
    WebSocketSettings,
    apply_cli_overrides,
    generate_default_config,
    load_config,
    load_yaml,
    save_config,
)
from pydantic import ValidationError


class TestWebSocketSettings:
    """Tests for WebSocketSettings configuration."""

    def test_default_values(self):
        """Test default WebSocket settings."""
        settings = WebSocketSettings()
        assert settings.enabled is True
        assert settings.port == 8766
        assert settings.ping_interval == 30
        assert settings.ping_timeout == 10

    def test_custom_values(self):
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

    def test_port_validation_too_low(self):
        """Test port validation rejects ports below 1024."""
        with pytest.raises(ValidationError):
            WebSocketSettings(port=80)

    def test_port_validation_too_high(self):
        """Test port validation rejects ports above 65535."""
        with pytest.raises(ValidationError):
            WebSocketSettings(port=70000)

    def test_ping_interval_must_be_positive(self):
        """Test ping_interval must be positive."""
        with pytest.raises(ValidationError):
            WebSocketSettings(ping_interval=0)


class TestLoggingSettings:
    """Tests for LoggingSettings configuration."""

    def test_default_values(self):
        """Test default logging settings."""
        settings = LoggingSettings()
        assert settings.level == "info"
        assert settings.format == "text"
        assert settings.max_size_mb == 10
        assert settings.backup_count == 5

    def test_valid_levels(self):
        """Test valid log levels."""
        for level in ["debug", "info", "warning", "error"]:
            settings = LoggingSettings(level=level)
            assert settings.level == level

    def test_invalid_level(self):
        """Test invalid log level raises error."""
        with pytest.raises(ValidationError):
            LoggingSettings(level="invalid")

    def test_max_size_must_be_positive(self):
        """Test max_size_mb must be positive."""
        with pytest.raises(ValidationError):
            LoggingSettings(max_size_mb=0)


class TestSessionSummaryConfig:
    """Tests for SessionSummaryConfig."""

    def test_default_values(self):
        """Test default session summary config."""
        config = SessionSummaryConfig()
        assert config.enabled is True
        assert config.provider == "claude"
        assert config.model == "claude-haiku-4-5"
        assert config.prompt is None

    def test_custom_values(self):
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


class TestCodeExecutionConfig:
    """Tests for CodeExecutionConfig."""

    def test_default_values(self):
        """Test default code execution config."""
        config = CodeExecutionConfig()
        assert config.enabled is True
        assert config.provider == "claude"
        assert config.model == "claude-sonnet-4-5"
        assert config.max_turns == 5
        assert config.default_timeout == 30

    def test_max_turns_validation(self):
        """Test max_turns must be positive."""
        with pytest.raises(ValidationError):
            CodeExecutionConfig(max_turns=0)

    def test_timeout_validation(self):
        """Test timeout must be positive."""
        with pytest.raises(ValidationError):
            CodeExecutionConfig(default_timeout=0)


class TestMCPClientProxyConfig:
    """Tests for MCPClientProxyConfig."""

    def test_default_values(self):
        """Test default MCP client proxy config."""
        config = MCPClientProxyConfig()
        assert config.enabled is True
        assert config.proxy_timeout == 30
        assert config.tool_timeout == 30

    def test_timeout_validation(self):
        """Test timeouts must be positive."""
        with pytest.raises(ValidationError):
            MCPClientProxyConfig(proxy_timeout=0)

        with pytest.raises(ValidationError):
            MCPClientProxyConfig(tool_timeout=-1)


class TestLLMProviderConfig:
    """Tests for LLMProviderConfig."""

    def test_models_list(self):
        """Test getting models as list."""
        config = LLMProviderConfig(
            models="model-a, model-b, model-c",
        )
        models = config.get_models_list()
        assert models == ["model-a", "model-b", "model-c"]

    def test_empty_models_in_list(self):
        """Test empty model entries are filtered."""
        config = LLMProviderConfig(
            models="model-a, , model-b",
        )
        models = config.get_models_list()
        assert models == ["model-a", "model-b"]


class TestLLMProvidersConfig:
    """Tests for LLMProvidersConfig."""

    def test_default_empty(self):
        """Test default config has no providers."""
        config = LLMProvidersConfig()
        assert config.get_enabled_providers() == []

    def test_enabled_providers(self):
        """Test listing enabled providers."""
        config = LLMProvidersConfig(
            claude=LLMProviderConfig(models="claude-haiku-4-5"),
            gemini=LLMProviderConfig(models="gemini-2.0-flash"),
        )
        providers = config.get_enabled_providers()
        assert "claude" in providers
        assert "gemini" in providers
        assert len(providers) == 2


class TestDaemonConfig:
    """Tests for DaemonConfig."""

    def test_default_values(self):
        """Test default daemon config."""
        config = DaemonConfig()
        assert config.daemon_port == 8765
        assert config.daemon_health_check_interval == 10.0
        assert config.database_path == "~/.gobby/gobby.db"

    def test_port_validation(self):
        """Test daemon port validation."""
        with pytest.raises(ValidationError):
            DaemonConfig(daemon_port=80)

        with pytest.raises(ValidationError):
            DaemonConfig(daemon_port=70000)

    def test_health_check_interval_validation(self):
        """Test health check interval validation."""
        with pytest.raises(ValidationError):
            DaemonConfig(daemon_health_check_interval=0.5)

        with pytest.raises(ValidationError):
            DaemonConfig(daemon_health_check_interval=500.0)

    def test_sub_config_access(self):
        """Test accessing sub-configurations."""
        config = DaemonConfig()
        assert config.get_code_execution_config() == config.code_execution
        assert config.get_recommend_tools_config() == config.recommend_tools
        assert config.get_mcp_client_proxy_config() == config.mcp_client_proxy


class TestLoadYaml:
    """Tests for load_yaml function."""

    def test_load_yaml_file(self, temp_dir: Path):
        """Test loading YAML file."""
        config_file = temp_dir / "config.yaml"
        config_file.write_text(
            yaml.dump({"daemon_port": 9000, "logging": {"level": "debug"}})
        )

        data = load_yaml(str(config_file))
        assert data["daemon_port"] == 9000
        assert data["logging"]["level"] == "debug"

    def test_load_json_file(self, temp_dir: Path):
        """Test loading JSON file."""
        config_file = temp_dir / "config.json"
        config_file.write_text(json.dumps({"daemon_port": 9001}))

        data = load_yaml(str(config_file))
        assert data["daemon_port"] == 9001

    def test_load_nonexistent_file(self, temp_dir: Path):
        """Test loading nonexistent file returns empty dict."""
        data = load_yaml(str(temp_dir / "nonexistent.yaml"))
        assert data == {}

    def test_load_empty_file(self, temp_dir: Path):
        """Test loading empty file returns empty dict."""
        config_file = temp_dir / "empty.yaml"
        config_file.write_text("")

        data = load_yaml(str(config_file))
        assert data == {}

    def test_invalid_extension(self, temp_dir: Path):
        """Test invalid file extension raises error."""
        config_file = temp_dir / "config.txt"
        config_file.write_text("key: value")

        with pytest.raises(ValueError, match="extension"):
            load_yaml(str(config_file))

    def test_invalid_yaml(self, temp_dir: Path):
        """Test invalid YAML raises error."""
        config_file = temp_dir / "invalid.yaml"
        config_file.write_text("invalid: yaml: content: [")

        with pytest.raises(ValueError, match="Invalid YAML"):
            load_yaml(str(config_file))


class TestApplyCliOverrides:
    """Tests for apply_cli_overrides function."""

    def test_simple_override(self):
        """Test simple key override."""
        config = {"daemon_port": 8765}
        overrides = {"daemon_port": 9000}

        result = apply_cli_overrides(config, overrides)
        assert result["daemon_port"] == 9000

    def test_nested_override(self):
        """Test nested key override with dot notation."""
        config = {"logging": {"level": "info"}}
        overrides = {"logging.level": "debug"}

        result = apply_cli_overrides(config, overrides)
        assert result["logging"]["level"] == "debug"

    def test_creates_nested_path(self):
        """Test creating nested path that doesn't exist."""
        config = {}
        overrides = {"logging.level": "debug"}

        result = apply_cli_overrides(config, overrides)
        assert result["logging"]["level"] == "debug"

    def test_none_overrides(self):
        """Test None overrides returns config unchanged."""
        config = {"key": "value"}
        result = apply_cli_overrides(config, None)
        assert result == config


class TestLoadConfig:
    """Tests for load_config function."""

    def test_load_default_config(self, temp_dir: Path, monkeypatch):
        """Test loading default config when no file exists."""
        monkeypatch.chdir(temp_dir)
        config = load_config(config_file=str(temp_dir / "nonexistent.yaml"))
        assert isinstance(config, DaemonConfig)

    def test_load_with_yaml_file(self, temp_dir: Path):
        """Test loading config from YAML file."""
        config_file = temp_dir / "config.yaml"
        config_file.write_text(yaml.dump({"daemon_port": 9000}))

        config = load_config(config_file=str(config_file))
        assert config.daemon_port == 9000

    def test_load_with_cli_overrides(self, temp_dir: Path):
        """Test loading config with CLI overrides."""
        config_file = temp_dir / "config.yaml"
        config_file.write_text(yaml.dump({"daemon_port": 8000}))

        config = load_config(
            config_file=str(config_file),
            cli_overrides={"daemon_port": 9000},
        )
        assert config.daemon_port == 9000

    def test_create_default_config(self, temp_dir: Path):
        """Test creating default config file."""
        config_file = temp_dir / "new_config.yaml"
        assert not config_file.exists()

        load_config(config_file=str(config_file), create_default=True)
        assert config_file.exists()


class TestGenerateDefaultConfig:
    """Tests for generate_default_config function."""

    def test_generates_file(self, temp_dir: Path):
        """Test generating default config file."""
        config_file = temp_dir / "generated.yaml"
        generate_default_config(str(config_file))

        assert config_file.exists()
        content = yaml.safe_load(config_file.read_text())
        assert "daemon_port" in content
        assert "websocket" in content
        assert "logging" in content

    def test_creates_parent_directory(self, temp_dir: Path):
        """Test creating parent directory for config file."""
        config_file = temp_dir / "subdir" / "config.yaml"
        generate_default_config(str(config_file))

        assert config_file.exists()


class TestSaveConfig:
    """Tests for save_config function."""

    def test_saves_config(self, temp_dir: Path, default_config: DaemonConfig):
        """Test saving config to file."""
        config_file = temp_dir / "saved.yaml"
        save_config(default_config, str(config_file))

        assert config_file.exists()
        content = yaml.safe_load(config_file.read_text())
        assert content["daemon_port"] == default_config.daemon_port

    def test_file_permissions(self, temp_dir: Path, default_config: DaemonConfig):
        """Test saved config has restrictive permissions."""
        config_file = temp_dir / "secure.yaml"
        save_config(default_config, str(config_file))

        # Check permissions (0o600 = owner read/write only)
        mode = config_file.stat().st_mode & 0o777
        assert mode == 0o600

    def test_creates_parent_directory(self, temp_dir: Path, default_config: DaemonConfig):
        """Test creating parent directory when saving."""
        config_file = temp_dir / "nested" / "dir" / "config.yaml"
        save_config(default_config, str(config_file))

        assert config_file.exists()
