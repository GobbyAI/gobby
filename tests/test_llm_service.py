"""Tests for the LLMService multi-provider support."""

from unittest.mock import MagicMock, patch

import pytest
from gobby.config.app import (
    DaemonConfig,
    LLMProviderConfig,
    LLMProvidersConfig,
    SessionSummaryConfig,
)
from gobby.llm.service import LLMService


@pytest.fixture
def llm_config() -> DaemonConfig:
    """Create a DaemonConfig with LLM providers configured."""
    return DaemonConfig(
        llm_providers=LLMProvidersConfig(
            claude=LLMProviderConfig(models="claude-haiku-4-5, claude-sonnet-4-5"),
            gemini=LLMProviderConfig(models="gemini-2.0-flash"),
        ),
    )


@pytest.fixture
def llm_config_empty_providers() -> DaemonConfig:
    """Create a DaemonConfig with empty LLM providers."""
    return DaemonConfig(llm_providers=LLMProvidersConfig())


@pytest.fixture
def llm_config_claude_only() -> DaemonConfig:
    """Create a DaemonConfig with only Claude configured."""
    return DaemonConfig(
        llm_providers=LLMProvidersConfig(
            claude=LLMProviderConfig(models="claude-haiku-4-5"),
        ),
    )


class TestLLMServiceInit:
    """Tests for LLMService initialization."""

    def test_init_with_valid_config(self, llm_config: DaemonConfig):
        """Test initialization with valid configuration."""
        service = LLMService(llm_config)

        assert service._config == llm_config
        assert service._providers == {}
        assert service._initialized_providers == set()

    def test_init_with_empty_providers_succeeds(self, llm_config_empty_providers: DaemonConfig):
        """Test initialization succeeds with empty providers (validation happens later)."""
        # Empty LLMProvidersConfig is still a valid config object
        # Errors occur when trying to get a provider
        service = LLMService(llm_config_empty_providers)
        assert service.enabled_providers == []


class TestLLMServiceGetProvider:
    """Tests for get_provider method."""

    def test_get_provider_unconfigured_raises(self, llm_config_claude_only: DaemonConfig):
        """Test getting an unconfigured provider raises error."""
        service = LLMService(llm_config_claude_only)

        with pytest.raises(ValueError, match="Provider 'gemini' is not configured"):
            service.get_provider("gemini")

    def test_get_provider_unknown_raises(self, llm_config: DaemonConfig):
        """Test getting an unknown provider raises error."""
        service = LLMService(llm_config)

        # "invalid" is not a configured provider, so should raise
        with pytest.raises(ValueError, match="is not configured"):
            service.get_provider("invalid")

    @patch("gobby.llm.claude.ClaudeLLMProvider")
    def test_get_provider_claude(
        self, mock_claude_provider: MagicMock, llm_config_claude_only: DaemonConfig
    ):
        """Test getting Claude provider creates instance."""
        mock_instance = MagicMock()
        mock_claude_provider.return_value = mock_instance

        service = LLMService(llm_config_claude_only)
        provider = service.get_provider("claude")

        assert provider == mock_instance
        mock_claude_provider.assert_called_once_with(llm_config_claude_only)

    @patch("gobby.llm.claude.ClaudeLLMProvider")
    def test_get_provider_caches_instance(
        self, mock_claude_provider: MagicMock, llm_config_claude_only: DaemonConfig
    ):
        """Test that get_provider caches provider instances."""
        mock_instance = MagicMock()
        mock_claude_provider.return_value = mock_instance

        service = LLMService(llm_config_claude_only)

        # First call creates the provider
        provider1 = service.get_provider("claude")
        # Second call should return cached instance
        provider2 = service.get_provider("claude")

        assert provider1 is provider2
        # Should only be called once due to caching
        mock_claude_provider.assert_called_once()


class TestLLMServiceGetProviderForFeature:
    """Tests for get_provider_for_feature method."""

    def test_get_provider_for_feature_missing_provider(self, llm_config: DaemonConfig):
        """Test error when feature config missing provider field."""
        service = LLMService(llm_config)

        feature_config = MagicMock()
        feature_config.provider = None
        feature_config.model = "claude-haiku-4-5"

        with pytest.raises(ValueError, match="missing 'provider' field"):
            service.get_provider_for_feature(feature_config)

    def test_get_provider_for_feature_missing_model(self, llm_config: DaemonConfig):
        """Test error when feature config missing model field."""
        service = LLMService(llm_config)

        feature_config = MagicMock()
        feature_config.provider = "claude"
        feature_config.model = None

        with pytest.raises(ValueError, match="missing 'model' field"):
            service.get_provider_for_feature(feature_config)

    @patch("gobby.llm.claude.ClaudeLLMProvider")
    def test_get_provider_for_feature_success(
        self, mock_claude_provider: MagicMock, llm_config_claude_only: DaemonConfig
    ):
        """Test successful feature provider lookup."""
        mock_instance = MagicMock()
        mock_claude_provider.return_value = mock_instance

        service = LLMService(llm_config_claude_only)

        # Create feature config with provider, model, and prompt
        feature_config = SessionSummaryConfig(
            provider="claude",
            model="claude-haiku-4-5",
            prompt="Test prompt {transcript_summary}",
        )

        provider, model, prompt = service.get_provider_for_feature(feature_config)

        assert provider == mock_instance
        assert model == "claude-haiku-4-5"
        assert prompt == "Test prompt {transcript_summary}"

    @patch("gobby.llm.claude.ClaudeLLMProvider")
    def test_get_provider_for_feature_no_prompt(
        self, mock_claude_provider: MagicMock, llm_config_claude_only: DaemonConfig
    ):
        """Test feature provider lookup when prompt is None."""
        mock_instance = MagicMock()
        mock_claude_provider.return_value = mock_instance

        service = LLMService(llm_config_claude_only)

        feature_config = SessionSummaryConfig(
            provider="claude",
            model="claude-haiku-4-5",
            prompt=None,  # No prompt configured
        )

        provider, model, prompt = service.get_provider_for_feature(feature_config)

        assert provider == mock_instance
        assert model == "claude-haiku-4-5"
        assert prompt is None


class TestLLMServiceGetDefaultProvider:
    """Tests for get_default_provider method."""

    @patch("gobby.llm.claude.ClaudeLLMProvider")
    def test_get_default_provider_prefers_claude(
        self, mock_claude_provider: MagicMock, llm_config: DaemonConfig
    ):
        """Test default provider prefers Claude when available."""
        mock_instance = MagicMock()
        mock_claude_provider.return_value = mock_instance

        service = LLMService(llm_config)
        provider = service.get_default_provider()

        assert provider == mock_instance

    @patch("gobby.llm.gemini.GeminiProvider")
    def test_get_default_provider_fallback(self, mock_gemini_provider: MagicMock):
        """Test default provider falls back to first available when Claude not configured."""
        mock_instance = MagicMock()
        mock_gemini_provider.return_value = mock_instance

        # Config with only Gemini
        config = DaemonConfig(
            llm_providers=LLMProvidersConfig(
                gemini=LLMProviderConfig(models="gemini-2.0-flash"),
            ),
        )

        service = LLMService(config)
        provider = service.get_default_provider()

        assert provider == mock_instance

    def test_get_default_provider_no_enabled_raises(self):
        """Test error when no providers are enabled."""
        # Create config with empty llm_providers
        config = DaemonConfig(llm_providers=LLMProvidersConfig())
        service = LLMService(config)

        # Trying to get a default provider when none are enabled should raise
        with pytest.raises(ValueError, match="No providers configured"):
            service.get_default_provider()


class TestLLMServiceProperties:
    """Tests for LLMService properties."""

    def test_enabled_providers(self, llm_config: DaemonConfig):
        """Test enabled_providers property."""
        service = LLMService(llm_config)

        enabled = service.enabled_providers
        assert "claude" in enabled
        assert "gemini" in enabled
        assert len(enabled) == 2

    @patch("gobby.llm.claude.ClaudeLLMProvider")
    def test_initialized_providers(
        self, mock_claude_provider: MagicMock, llm_config_claude_only: DaemonConfig
    ):
        """Test initialized_providers property."""
        mock_claude_provider.return_value = MagicMock()

        service = LLMService(llm_config_claude_only)

        # Initially empty
        assert service.initialized_providers == []

        # After getting a provider
        service.get_provider("claude")
        assert "claude" in service.initialized_providers

    def test_repr(self, llm_config: DaemonConfig):
        """Test string representation."""
        service = LLMService(llm_config)

        repr_str = repr(service)
        assert "LLMService" in repr_str
        assert "enabled=" in repr_str
        assert "initialized=" in repr_str
