"""Tests for the LiteLLMProvider LLM implementation."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.config.app import (
    DaemonConfig,
    LLMProviderConfig,
    LLMProvidersConfig,
    SessionSummaryConfig,
    TitleSynthesisConfig,
)


@pytest.fixture
def litellm_config() -> DaemonConfig:
    """Create a DaemonConfig with LiteLLM provider configured."""
    return DaemonConfig(
        llm_providers=LLMProvidersConfig(
            litellm=LLMProviderConfig(models="gpt-4o-mini,mistral-large"),
            api_keys={"OPENAI_API_KEY": "sk-test-key", "MISTRAL_API_KEY": "mistral-test"},
        ),
        session_summary=SessionSummaryConfig(model="gpt-4o-mini"),
        title_synthesis=TitleSynthesisConfig(model="gpt-4o-mini"),
    )


@pytest.fixture
def litellm_config_no_keys() -> DaemonConfig:
    """Create a DaemonConfig with LiteLLM but no API keys."""
    return DaemonConfig(
        llm_providers=LLMProvidersConfig(
            litellm=LLMProviderConfig(models="gpt-4o-mini"),
        ),
    )


class TestLiteLLMProviderInit:
    """Tests for LiteLLMProvider initialization."""

    def test_init_with_api_keys(self, litellm_config: DaemonConfig):
        """Test initialization with API keys in config."""
        from gobby.llm.litellm import LiteLLMProvider
        provider = LiteLLMProvider(litellm_config)

        assert provider.provider_name == "litellm"
        assert provider.auth_mode == "api_key"
        assert "OPENAI_API_KEY" in provider._api_keys
        assert provider._api_keys["OPENAI_API_KEY"] == "sk-test-key"

    def test_init_without_api_keys(self, litellm_config_no_keys: DaemonConfig):
        """Test initialization without API keys."""
        from gobby.llm.litellm import LiteLLMProvider
        provider = LiteLLMProvider(litellm_config_no_keys)

        assert provider._api_keys == {}


class TestLiteLLMProviderProperties:
    """Tests for LiteLLMProvider properties."""

    def test_provider_name(self, litellm_config: DaemonConfig):
        """Test provider_name property."""
        from gobby.llm.litellm import LiteLLMProvider
        provider = LiteLLMProvider(litellm_config)
        assert provider.provider_name == "litellm"

    def test_auth_mode(self, litellm_config: DaemonConfig):
        """Test auth_mode property always returns api_key."""
        from gobby.llm.litellm import LiteLLMProvider
        provider = LiteLLMProvider(litellm_config)
        assert provider.auth_mode == "api_key"

    def test_get_model_summary(self, litellm_config: DaemonConfig):
        """Test _get_model for summary task."""
        from gobby.llm.litellm import LiteLLMProvider
        provider = LiteLLMProvider(litellm_config)
        assert provider._get_model("summary") == "gpt-4o-mini"

    def test_get_model_title(self, litellm_config: DaemonConfig):
        """Test _get_model for title task."""
        from gobby.llm.litellm import LiteLLMProvider
        provider = LiteLLMProvider(litellm_config)
        assert provider._get_model("title") == "gpt-4o-mini"

    def test_get_model_unknown(self, litellm_config: DaemonConfig):
        """Test _get_model for unknown task defaults to gpt-4o-mini."""
        from gobby.llm.litellm import LiteLLMProvider
        provider = LiteLLMProvider(litellm_config)
        assert provider._get_model("unknown") == "gpt-4o-mini"


class TestLiteLLMProviderGenerateSummary:
    """Tests for generate_summary method."""

    @pytest.mark.asyncio
    async def test_generate_summary_no_litellm(self, litellm_config: DaemonConfig):
        """Test generate_summary returns error when litellm not initialized."""
        from gobby.llm.litellm import LiteLLMProvider
        provider = LiteLLMProvider(litellm_config)
        provider._litellm = None

        result = await provider.generate_summary(
            {"transcript_summary": "test"},
            prompt_template="Test {transcript_summary}"
        )

        assert "unavailable" in result

    @pytest.mark.asyncio
    async def test_generate_summary_no_template(self, litellm_config: DaemonConfig):
        """Test generate_summary raises when no template provided."""
        from gobby.llm.litellm import LiteLLMProvider
        provider = LiteLLMProvider(litellm_config)

        with pytest.raises(ValueError, match="prompt_template is required"):
            await provider.generate_summary({"transcript_summary": "test"})


class TestLiteLLMProviderSynthesizeTitle:
    """Tests for synthesize_title method."""

    @pytest.mark.asyncio
    async def test_synthesize_title_no_litellm(self, litellm_config: DaemonConfig):
        """Test synthesize_title returns None when litellm not initialized."""
        from gobby.llm.litellm import LiteLLMProvider
        provider = LiteLLMProvider(litellm_config)
        provider._litellm = None

        result = await provider.synthesize_title(
            "test prompt",
            prompt_template="Generate title: {user_prompt}"
        )

        assert result is None

    @pytest.mark.asyncio
    async def test_synthesize_title_no_template(self, litellm_config: DaemonConfig):
        """Test synthesize_title raises when no template provided."""
        from gobby.llm.litellm import LiteLLMProvider
        provider = LiteLLMProvider(litellm_config)

        with pytest.raises(ValueError, match="prompt_template is required"):
            await provider.synthesize_title("test prompt")


class TestLiteLLMProviderExecuteCode:
    """Tests for execute_code method."""

    @pytest.mark.asyncio
    async def test_execute_code_not_supported(self, litellm_config: DaemonConfig):
        """Test execute_code returns not supported error."""
        from gobby.llm.litellm import LiteLLMProvider
        provider = LiteLLMProvider(litellm_config)

        result = await provider.execute_code("print('hello')")

        assert result["success"] is False
        assert "not supported" in result["error"]
        assert "LiteLLM" in result["error"]
        assert result["language"] == "python"

    @pytest.mark.asyncio
    async def test_execute_code_different_language(self, litellm_config: DaemonConfig):
        """Test execute_code returns not supported for any language."""
        from gobby.llm.litellm import LiteLLMProvider
        provider = LiteLLMProvider(litellm_config)

        result = await provider.execute_code(
            "console.log('hello')",
            language="javascript"
        )

        assert result["success"] is False
        assert result["language"] == "javascript"
