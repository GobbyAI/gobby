"""Tests for the LLMService multi-provider support."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.config.app import DaemonConfig, LocalConfig
from gobby.config.feature_base import ModelTier
from gobby.config.llm_providers import LLMProviderConfig, LLMProvidersConfig
from gobby.config.sessions import DigestConfig, SessionSummaryConfig
from gobby.llm.service import LLMService

pytestmark = pytest.mark.unit


@pytest.fixture
def llm_config() -> DaemonConfig:
    """Create a DaemonConfig with LLM providers configured."""
    return DaemonConfig(
        llm_providers=LLMProvidersConfig(
            claude=LLMProviderConfig(models="claude-haiku-4-5, claude-sonnet-4-5"),
        ),
    )


@pytest.fixture
def llm_config_empty_providers() -> DaemonConfig:
    """Create a DaemonConfig with empty LLM providers."""
    return DaemonConfig(llm_providers=LLMProvidersConfig(claude=None))


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

    def test_init_with_valid_config(self, llm_config: DaemonConfig) -> None:
        """Test initialization with valid configuration."""
        service = LLMService(llm_config)

        assert service._config == llm_config
        assert service._providers == {}
        assert service._initialized_providers == set()

    def test_init_with_empty_providers_succeeds(
        self, llm_config_empty_providers: DaemonConfig
    ) -> None:
        """Test initialization succeeds with empty providers (validation happens later)."""
        # Empty LLMProvidersConfig is still a valid config object
        # Errors occur when trying to get a provider
        service = LLMService(llm_config_empty_providers)
        assert service.enabled_providers == []


class TestLLMServiceGetProvider:
    """Tests for get_provider method."""

    def test_get_provider_unconfigured_raises(self, llm_config_claude_only: DaemonConfig) -> None:
        """Test getting an unconfigured provider raises error."""
        service = LLMService(llm_config_claude_only)

        with pytest.raises(ValueError, match="Provider 'codex' is not configured"):
            service.get_provider("codex")

    def test_get_provider_unknown_raises(self, llm_config: DaemonConfig) -> None:
        """Test getting an unknown provider raises error."""
        service = LLMService(llm_config)

        # "invalid" is not a configured provider, so should raise
        with pytest.raises(ValueError, match="is not configured"):
            service.get_provider("invalid")

    @patch("gobby.llm.claude.ClaudeLLMProvider")
    def test_get_provider_claude(
        self, mock_claude_provider: MagicMock, llm_config_claude_only: DaemonConfig
    ) -> None:
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
    ) -> None:
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

    def test_get_provider_for_feature_missing_provider(self, llm_config: DaemonConfig) -> None:
        """Test error when feature config missing provider field."""
        service = LLMService(llm_config)

        feature_config = MagicMock()
        feature_config.provider = None
        feature_config.model = "claude-haiku-4-5"

        with pytest.raises(ValueError, match="missing 'provider' field"):
            service.get_provider_for_feature(feature_config)

    def test_get_provider_for_feature_missing_model(self, llm_config: DaemonConfig) -> None:
        """Test error when feature config missing model field."""
        service = LLMService(llm_config)

        feature_config = MagicMock()
        feature_config.provider = "claude"
        feature_config.model = None

        with pytest.raises(ValueError, match="missing 'model' field"):
            service.get_provider_for_feature(feature_config)

    def test_get_provider_for_feature_rejects_claude_alias_on_non_claude_provider(
        self, llm_config: DaemonConfig
    ) -> None:
        """Claude shorthand aliases are only valid for provider='claude'."""
        service = LLMService(llm_config)

        feature_config = MagicMock()
        feature_config.provider = "codex"
        feature_config.model = "sonnet"
        feature_config.prompt = None

        with pytest.raises(ValueError, match="Only provider='claude' accepts haiku/sonnet/opus"):
            service.get_provider_for_feature(feature_config)

    @patch("gobby.llm.claude.ClaudeLLMProvider")
    def test_get_provider_for_feature_success(
        self, mock_claude_provider: MagicMock, llm_config_claude_only: DaemonConfig
    ) -> None:
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
    ) -> None:
        """Test feature provider lookup when prompt is None."""
        mock_instance = MagicMock()
        mock_claude_provider.return_value = mock_instance

        service = LLMService(llm_config_claude_only)

        feature_config = MagicMock()
        feature_config.provider = "claude"
        feature_config.model = "claude-haiku-4-5"
        feature_config.prompt = None

        provider, model, prompt = service.get_provider_for_feature(feature_config)

        assert provider == mock_instance
        assert model == "claude-haiku-4-5"
        assert prompt is None


class TestLLMServiceGetDefaultProvider:
    """Tests for get_default_provider method."""

    @patch("gobby.llm.claude.ClaudeLLMProvider")
    def test_get_default_provider_prefers_claude(
        self, mock_claude_provider: MagicMock, llm_config: DaemonConfig
    ) -> None:
        """Test default provider prefers Claude when available."""
        mock_instance = MagicMock()
        mock_claude_provider.return_value = mock_instance

        service = LLMService(llm_config)
        provider = service.get_default_provider()

        assert provider == mock_instance

    def test_get_default_provider_no_enabled_raises(self) -> None:
        """Test error when no providers are enabled."""
        # Create config with empty llm_providers
        config = DaemonConfig(llm_providers=LLMProvidersConfig(claude=None))
        service = LLMService(config)

        # Trying to get a default provider when none are enabled should raise
        with pytest.raises(ValueError, match="No providers configured"):
            service.get_default_provider()


class TestLLMServiceProperties:
    """Tests for LLMService properties."""

    def test_enabled_providers(self, llm_config: DaemonConfig) -> None:
        """Test enabled_providers property."""
        service = LLMService(llm_config)

        enabled = service.enabled_providers
        assert "claude" in enabled
        assert len(enabled) == 1

    @patch("gobby.llm.claude.ClaudeLLMProvider")
    def test_initialized_providers(
        self, mock_claude_provider: MagicMock, llm_config_claude_only: DaemonConfig
    ) -> None:
        """Test initialized_providers property."""
        mock_claude_provider.return_value = MagicMock()

        service = LLMService(llm_config_claude_only)

        # Initially empty
        assert service.initialized_providers == []

        # After getting a provider
        service.get_provider("claude")
        assert "claude" in service.initialized_providers

    def test_repr(self, llm_config: DaemonConfig) -> None:
        """Test string representation."""
        service = LLMService(llm_config)

        repr_str = repr(service)
        assert "LLMService" in repr_str
        assert "enabled=" in repr_str
        assert "initialized=" in repr_str


# ═══════════════════════════════════════════════════════════════════════
# Local provider integration
# ═══════════════════════════════════════════════════════════════════════


class TestLLMServiceLocalProvider:
    """Tests for local provider wiring in LLMService."""

    @pytest.fixture
    def llm_config_with_local(self) -> DaemonConfig:
        return DaemonConfig(
            llm_providers=LLMProvidersConfig(
                claude=LLMProviderConfig(models="haiku,sonnet,opus"),
            ),
            local=LocalConfig(url="http://localhost:1234/v1", model="test-model"),
        )

    @patch("openai.AsyncOpenAI")
    def test_get_provider_local(
        self, mock_openai: MagicMock, llm_config_with_local: DaemonConfig
    ) -> None:
        from gobby.llm.local import LocalLLMProvider

        service = LLMService(llm_config_with_local)
        provider = service.get_provider("local")
        assert isinstance(provider, LocalLLMProvider)

    @patch("openai.AsyncOpenAI")
    def test_get_provider_local_cached(
        self, mock_openai: MagicMock, llm_config_with_local: DaemonConfig
    ) -> None:
        service = LLMService(llm_config_with_local)
        p1 = service.get_provider("local")
        p2 = service.get_provider("local")
        assert p1 is p2

    def test_get_provider_local_not_configured_raises(self, llm_config: DaemonConfig) -> None:
        """local=None in config should raise ValueError."""
        service = LLMService(llm_config)
        with pytest.raises(ValueError, match="local"):
            service.get_provider("local")

    def test_enabled_providers_includes_local(self, llm_config_with_local: DaemonConfig) -> None:
        service = LLMService(llm_config_with_local)
        assert "local" in service.enabled_providers

    def test_enabled_providers_excludes_local_when_not_configured(
        self, llm_config: DaemonConfig
    ) -> None:
        service = LLMService(llm_config)
        assert "local" not in service.enabled_providers


class TestLLMServiceCallFeature:
    """Tests for call_feature method with tier-based fallback."""

    @pytest.fixture
    def llm_config_with_local(self) -> DaemonConfig:
        return DaemonConfig(
            llm_providers=LLMProvidersConfig(
                claude=LLMProviderConfig(models="haiku,sonnet,opus"),
            ),
            local=LocalConfig(url="http://localhost:1234/v1", model="test-model"),
        )

    @pytest.mark.asyncio
    @patch("openai.AsyncOpenAI")
    async def test_call_feature_success(
        self, mock_openai: MagicMock, llm_config_with_local: DaemonConfig
    ) -> None:
        """call_feature routes through local provider successfully."""
        service = LLMService(llm_config_with_local)
        local_provider = service.get_provider("local")
        local_provider.generate_text = AsyncMock(return_value="Fix Auth Bug")

        config = DigestConfig(provider="local", model="test-model")
        result = await service.call_feature(
            config,
            "prompt text",
            caller="memory.title_synthesis",
        )
        assert result == "Fix Auth Bug"
        assert local_provider.generate_text.await_args.kwargs["caller"] == "memory.title_synthesis"

    @pytest.mark.asyncio
    @patch("openai.AsyncOpenAI")
    async def test_call_json_feature_success(
        self, mock_openai: MagicMock, llm_config_with_local: DaemonConfig
    ) -> None:
        """call_json_feature routes prompt, system prompt, model, and caller."""
        service = LLMService(llm_config_with_local)
        local_provider = service.get_provider("local")
        local_provider.generate_json = AsyncMock(return_value={"entities": []})

        config = DigestConfig(provider="local", model="test-model")
        result = await service.call_json_feature(
            config,
            "rendered prompt",
            system_prompt="strict JSON system prompt",
            caller="memory.kg.extract_entities",
        )

        assert result == {"entities": []}
        local_provider.generate_json.assert_awaited_once_with(
            "rendered prompt",
            "strict JSON system prompt",
            "test-model",
            caller="memory.kg.extract_entities",
        )

    @pytest.mark.asyncio
    @patch("gobby.llm.claude.ClaudeLLMProvider")
    @patch("openai.AsyncOpenAI")
    async def test_call_json_feature_fallback_on_local_failure(
        self,
        mock_openai: MagicMock,
        mock_claude_cls: MagicMock,
        llm_config_with_local: DaemonConfig,
    ) -> None:
        """Local JSON feature failures fall back with the same instruction contract."""
        mock_claude_instance = MagicMock()
        mock_claude_instance.generate_json = AsyncMock(return_value={"entities": []})
        mock_claude_cls.return_value = mock_claude_instance

        service = LLMService(llm_config_with_local)
        local_provider = service.get_provider("local")
        local_provider.generate_json = AsyncMock(side_effect=RuntimeError("local down"))

        config = DigestConfig(provider="local", model="test-model")
        result = await service.call_json_feature(
            config,
            "rendered prompt",
            system_prompt="strict JSON system prompt",
            caller="memory.kg.extract_entities",
        )

        assert result == {"entities": []}
        assert result["entities"] == []
        mock_claude_instance.generate_json.assert_awaited_once_with(
            "rendered prompt",
            "strict JSON system prompt",
            "haiku",
            caller="memory.kg.extract_entities",
        )

    @pytest.mark.asyncio
    @patch("gobby.llm.claude.ClaudeLLMProvider")
    @patch("openai.AsyncOpenAI")
    async def test_call_json_feature_unexpected_local_failure_does_not_fallback(
        self,
        mock_openai: MagicMock,
        mock_claude_cls: MagicMock,
        llm_config_with_local: DaemonConfig,
    ) -> None:
        """Unexpected local JSON failures propagate without Claude fallback."""
        mock_claude_instance = MagicMock()
        mock_claude_instance.generate_json = AsyncMock(return_value={"entities": []})
        mock_claude_cls.return_value = mock_claude_instance

        service = LLMService(llm_config_with_local)
        local_provider = service.get_provider("local")
        local_provider.generate_json = AsyncMock(side_effect=ConnectionError("local down"))

        config = DigestConfig(provider="local", model="test-model")
        with pytest.raises(ConnectionError, match="local down") as exc_info:
            await service.call_json_feature(config, "rendered prompt")
        assert exc_info.value.args == ("local down",)
        mock_claude_instance.generate_json.assert_not_awaited()

    @pytest.mark.asyncio
    @patch("gobby.llm.claude.ClaudeLLMProvider")
    @patch("openai.AsyncOpenAI")
    async def test_call_feature_fallback_on_local_failure(
        self,
        mock_openai: MagicMock,
        mock_claude_cls: MagicMock,
        llm_config_with_local: DaemonConfig,
    ) -> None:
        """When local provider fails, falls back to Claude with tier model."""
        mock_claude_instance = MagicMock()
        mock_claude_instance.generate_text = AsyncMock(return_value="Fallback Title")
        mock_claude_cls.return_value = mock_claude_instance

        service = LLMService(llm_config_with_local)
        local_provider = service.get_provider("local")
        local_provider.generate_text = AsyncMock(side_effect=RuntimeError("local server down"))

        config = DigestConfig(provider="local", model="test-model")
        result = await service.call_feature(
            config,
            "prompt text",
            caller="memory.title_synthesis",
        )

        assert result == "Fallback Title"
        # Should fall back to haiku (LOW tier). Inspect by inspecting bound
        # arguments rather than positional index so the assertion survives
        # signature changes.
        mock_claude_instance.generate_text.assert_awaited_once()
        call_args = mock_claude_instance.generate_text.call_args
        # generate_text signature: (prompt, system_prompt, model, max_tokens)
        bound_model = call_args.kwargs.get("model")
        if bound_model is None and len(call_args.args) >= 3:
            bound_model = call_args.args[2]
        assert bound_model == "haiku"
        assert call_args.kwargs["caller"] == "memory.title_synthesis"

    @pytest.mark.asyncio
    @patch("gobby.llm.claude.ClaudeLLMProvider")
    @patch("openai.AsyncOpenAI")
    async def test_call_feature_unexpected_local_failure_does_not_fallback(
        self,
        mock_openai: MagicMock,
        mock_claude_cls: MagicMock,
        llm_config_with_local: DaemonConfig,
    ) -> None:
        """Unexpected local text failures propagate without Claude fallback."""
        mock_claude_instance = MagicMock()
        mock_claude_instance.generate_text = AsyncMock(return_value="Fallback Title")
        mock_claude_cls.return_value = mock_claude_instance

        service = LLMService(llm_config_with_local)
        local_provider = service.get_provider("local")
        local_provider.generate_text = AsyncMock(side_effect=ConnectionError("local down"))

        config = DigestConfig(provider="local", model="test-model")
        with pytest.raises(ConnectionError, match="local down") as exc_info:
            await service.call_feature(config, "prompt text")
        assert exc_info.value.args == ("local down",)
        mock_claude_instance.generate_text.assert_not_awaited()

    @pytest.mark.asyncio
    @patch("gobby.llm.claude.ClaudeLLMProvider")
    @patch("openai.AsyncOpenAI")
    async def test_call_feature_fallback_mid_tier(
        self,
        mock_openai: MagicMock,
        mock_claude_cls: MagicMock,
        llm_config_with_local: DaemonConfig,
    ) -> None:
        """MID tier features fall back to sonnet."""
        mock_claude_instance = MagicMock()
        mock_claude_instance.generate_text = AsyncMock(return_value="Summary")
        mock_claude_cls.return_value = mock_claude_instance

        service = LLMService(llm_config_with_local)
        local_provider = service.get_provider("local")
        local_provider.generate_text = AsyncMock(side_effect=RuntimeError("down"))

        config = SessionSummaryConfig(provider="local", model="test-model")
        assert config.tier == ModelTier.MID

        result = await service.call_feature(config, "prompt text")
        assert result == "Summary"
        call_args = mock_claude_instance.generate_text.call_args
        bound_model = call_args.kwargs.get("model")
        if bound_model is None and len(call_args.args) >= 3:
            bound_model = call_args.args[2]
        assert bound_model == "sonnet"

    @pytest.mark.asyncio
    @patch("gobby.llm.claude.ClaudeLLMProvider")
    async def test_call_feature_non_local_does_not_fallback(
        self, mock_claude_cls: MagicMock, llm_config: DaemonConfig
    ) -> None:
        """Non-local provider failures propagate normally."""
        mock_claude_instance = MagicMock()
        mock_claude_instance.generate_text = AsyncMock(side_effect=RuntimeError("claude error"))
        mock_claude_cls.return_value = mock_claude_instance

        service = LLMService(llm_config)
        config = DigestConfig(provider="claude", model="haiku")

        with pytest.raises(RuntimeError, match="claude error"):
            await service.call_feature(config, "prompt text")
