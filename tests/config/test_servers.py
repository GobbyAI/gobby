"""
Tests for config/servers.py module.

RED PHASE: Tests initially import from servers.py (should fail),
then will pass once WebSocketSettings and MCPClientProxyConfig are extracted from app.py.
"""

import pytest
from pydantic import ValidationError

pytestmark = pytest.mark.unit


class TestWebSocketSettingsImport:
    """Test that WebSocketSettings can be imported from the servers module."""

    def test_import_from_servers_module(self) -> None:
        """Test importing WebSocketSettings from config.servers (RED phase target)."""
        from gobby.config.servers import WebSocketSettings

        assert WebSocketSettings is not None


class TestWebSocketSettingsDefaults:
    """Test WebSocketSettings default values."""

    def test_default_instantiation(self) -> None:
        """Test WebSocketSettings creates with all defaults."""
        from gobby.config.servers import WebSocketSettings

        settings = WebSocketSettings()
        assert settings.enabled is True
        assert settings.port == 60888
        assert settings.ping_interval == 30
        assert settings.ping_timeout == 10

    def test_disabled_websocket(self) -> None:
        """Test WebSocketSettings with disabled WebSocket."""
        from gobby.config.servers import WebSocketSettings

        settings = WebSocketSettings(enabled=False)
        assert settings.enabled is False


class TestWebSocketSettingsCustomValues:
    """Test WebSocketSettings with custom values."""

    def test_custom_port(self) -> None:
        """Test setting custom port."""
        from gobby.config.servers import WebSocketSettings

        settings = WebSocketSettings(port=9000)
        assert settings.port == 9000

    def test_custom_ping_settings(self) -> None:
        """Test setting custom ping interval and timeout."""
        from gobby.config.servers import WebSocketSettings

        settings = WebSocketSettings(ping_interval=60, ping_timeout=20)
        assert settings.ping_interval == 60
        assert settings.ping_timeout == 20


class TestWebSocketSettingsValidation:
    """Test WebSocketSettings validation."""

    def test_port_must_be_in_valid_range(self) -> None:
        """Test that port must be between 1024 and 65535."""
        from gobby.config.servers import WebSocketSettings

        # Too low
        with pytest.raises(ValidationError) as exc_info:
            WebSocketSettings(port=1023)
        assert "1024" in str(exc_info.value) or "port" in str(exc_info.value).lower()

        # Too high
        with pytest.raises(ValidationError) as exc_info:
            WebSocketSettings(port=65536)
        assert "65535" in str(exc_info.value) or "port" in str(exc_info.value).lower()

    def test_port_valid_boundary(self) -> None:
        """Test port at valid boundary values."""
        from gobby.config.servers import WebSocketSettings

        # Minimum valid
        settings = WebSocketSettings(port=1024)
        assert settings.port == 1024

        # Maximum valid
        settings = WebSocketSettings(port=65535)
        assert settings.port == 65535

    def test_ping_interval_must_be_positive(self) -> None:
        """Test that ping_interval must be positive."""
        from gobby.config.servers import WebSocketSettings

        with pytest.raises(ValidationError) as exc_info:
            WebSocketSettings(ping_interval=0)
        assert "positive" in str(exc_info.value).lower()

        with pytest.raises(ValidationError) as exc_info:
            WebSocketSettings(ping_interval=-1)
        assert "positive" in str(exc_info.value).lower()

    def test_ping_timeout_must_be_positive(self) -> None:
        """Test that ping_timeout must be positive."""
        from gobby.config.servers import WebSocketSettings

        with pytest.raises(ValidationError) as exc_info:
            WebSocketSettings(ping_timeout=0)
        assert "positive" in str(exc_info.value).lower()


class TestMCPClientProxyConfigImport:
    """Test that MCPClientProxyConfig can be imported from the servers module."""

    def test_import_from_servers_module(self) -> None:
        """Test importing MCPClientProxyConfig from config.servers (RED phase target)."""
        from gobby.config.servers import MCPClientProxyConfig

        assert MCPClientProxyConfig is not None


class TestMCPClientProxyConfigDefaults:
    """Test MCPClientProxyConfig default values."""

    def test_default_instantiation(self) -> None:
        """Test MCPClientProxyConfig creates with all defaults."""
        from gobby.config.servers import MCPClientProxyConfig

        config = MCPClientProxyConfig()
        assert config.enabled is True
        assert config.connect_timeout == 30.0
        assert config.proxy_timeout == 30
        assert config.tool_timeout == 30
        assert config.tool_timeouts == {}
        assert config.search_mode == "llm"
        assert config.min_similarity == 0.3
        assert config.top_k == 10
        assert config.refresh_on_server_add is True
        assert config.refresh_timeout == 300.0


class TestMCPClientProxyConfigCustomValues:
    """Test MCPClientProxyConfig with custom values."""

    def test_custom_timeouts(self) -> None:
        """Test setting custom timeout values."""
        from gobby.config.servers import MCPClientProxyConfig

        config = MCPClientProxyConfig(
            connect_timeout=60.0,
            proxy_timeout=120,
            tool_timeout=45,
            refresh_timeout=600.0,
        )
        assert config.connect_timeout == 60.0
        assert config.proxy_timeout == 120
        assert config.tool_timeout == 45
        assert config.refresh_timeout == 600.0

    def test_custom_tool_timeouts(self) -> None:
        """Test setting tool-specific timeouts."""
        from gobby.config.servers import MCPClientProxyConfig

        config = MCPClientProxyConfig(tool_timeouts={"slow_tool": 120.0, "fast_tool": 5.0})
        assert config.tool_timeouts["slow_tool"] == 120.0
        assert config.tool_timeouts["fast_tool"] == 5.0

    def test_custom_search_settings(self) -> None:
        """Test setting custom search mode and similarity settings."""
        from gobby.config.servers import MCPClientProxyConfig

        config = MCPClientProxyConfig(
            search_mode="semantic",
            min_similarity=0.5,
            top_k=20,
        )
        assert config.search_mode == "semantic"
        assert config.min_similarity == 0.5
        assert config.top_k == 20

    def test_hybrid_search_mode(self) -> None:
        """Test hybrid search mode."""
        from gobby.config.servers import MCPClientProxyConfig

        config = MCPClientProxyConfig(search_mode="hybrid")
        assert config.search_mode == "hybrid"

    def test_embedding_fields_removed(self) -> None:
        """Embedding config consolidated to EmbeddingsConfig — fields removed from proxy."""
        from gobby.config.servers import MCPClientProxyConfig

        config = MCPClientProxyConfig()
        assert not hasattr(config, "embedding_provider")
        assert not hasattr(config, "embedding_model")
        assert not hasattr(config, "embedding_api_base")


class TestMCPClientProxyConfigValidation:
    """Test MCPClientProxyConfig validation."""

    def test_connect_timeout_must_be_positive(self) -> None:
        """Test that connect_timeout must be positive."""
        from gobby.config.servers import MCPClientProxyConfig

        with pytest.raises(ValidationError) as exc_info:
            MCPClientProxyConfig(connect_timeout=0)
        assert "positive" in str(exc_info.value).lower()

        with pytest.raises(ValidationError) as exc_info:
            MCPClientProxyConfig(connect_timeout=-1.0)
        assert "positive" in str(exc_info.value).lower()

    def test_proxy_timeout_must_be_positive(self) -> None:
        """Test that proxy_timeout must be positive."""
        from gobby.config.servers import MCPClientProxyConfig

        with pytest.raises(ValidationError) as exc_info:
            MCPClientProxyConfig(proxy_timeout=0)
        assert "positive" in str(exc_info.value).lower()

    def test_tool_timeout_must_be_positive(self) -> None:
        """Test that tool_timeout must be positive."""
        from gobby.config.servers import MCPClientProxyConfig

        with pytest.raises(ValidationError) as exc_info:
            MCPClientProxyConfig(tool_timeout=-5)
        assert "positive" in str(exc_info.value).lower()

    def test_min_similarity_must_be_in_range(self) -> None:
        """Test that min_similarity must be between 0 and 1."""
        from gobby.config.servers import MCPClientProxyConfig

        # Too low
        with pytest.raises(ValidationError) as exc_info:
            MCPClientProxyConfig(min_similarity=-0.1)
        assert "0" in str(exc_info.value) and "1" in str(exc_info.value)

        # Too high
        with pytest.raises(ValidationError) as exc_info:
            MCPClientProxyConfig(min_similarity=1.1)
        assert "0" in str(exc_info.value) and "1" in str(exc_info.value)

    def test_min_similarity_valid_boundaries(self) -> None:
        """Test min_similarity at valid boundary values."""
        from gobby.config.servers import MCPClientProxyConfig

        config = MCPClientProxyConfig(min_similarity=0.0)
        assert config.min_similarity == 0.0

        config = MCPClientProxyConfig(min_similarity=1.0)
        assert config.min_similarity == 1.0

    def test_top_k_must_be_positive(self) -> None:
        """Test that top_k must be positive."""
        from gobby.config.servers import MCPClientProxyConfig

        with pytest.raises(ValidationError) as exc_info:
            MCPClientProxyConfig(top_k=0)
        assert "positive" in str(exc_info.value).lower()

    def test_invalid_search_mode(self) -> None:
        """Test that invalid search mode raises ValidationError."""
        from gobby.config.servers import MCPClientProxyConfig

        with pytest.raises(ValidationError):
            MCPClientProxyConfig(search_mode="invalid")


class TestWebSocketSettingsFromAppPy:
    """Verify that tests pass when importing from app.py (reference implementation)."""

    def test_import_from_app_py(self) -> None:
        """Test importing WebSocketSettings from app.py works (baseline)."""
        from gobby.config.servers import WebSocketSettings

        settings = WebSocketSettings()
        assert settings.enabled is True
        assert settings.port == 60888

    def test_validation_via_app_py(self) -> None:
        """Test validation works when imported from app.py."""
        from gobby.config.servers import WebSocketSettings

        with pytest.raises(ValidationError):
            WebSocketSettings(port=100)  # Below valid range


class TestMCPClientProxyConfigFromAppPy:
    """Verify MCPClientProxyConfig tests pass when importing from app.py."""

    def test_import_from_app_py(self) -> None:
        """Test importing MCPClientProxyConfig from app.py works (baseline)."""
        from gobby.config.app import MCPClientProxyConfig

        config = MCPClientProxyConfig()
        assert config.enabled is True
        assert config.search_mode == "llm"

    def test_validation_via_app_py(self) -> None:
        """Test validation works when imported from app.py."""
        from gobby.config.app import MCPClientProxyConfig

        with pytest.raises(ValidationError):
            MCPClientProxyConfig(min_similarity=2.0)  # Above valid range
