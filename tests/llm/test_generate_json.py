"""Tests for generate_json() on LLM providers."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from gobby.config.app import DaemonConfig
from gobby.config.llm_providers import LLMProviderConfig, LLMProvidersConfig

pytestmark = pytest.mark.unit


@pytest.fixture
def litellm_config() -> DaemonConfig:
    """Create a DaemonConfig with LiteLLM provider configured."""
    return DaemonConfig(
        llm_providers=LLMProvidersConfig(
            litellm=LLMProviderConfig(models="gpt-4o-mini"),
            api_keys={"OPENAI_API_KEY": "sk-test-key"},
        ),
    )


class TestLLMProviderBaseGenerateJson:
    """Tests for LLMProvider abstract generate_json method."""

    def test_generate_json_is_abstract(self) -> None:
        """generate_json is declared on LLMProvider base class."""
        from gobby.llm.base import LLMProvider

        assert hasattr(LLMProvider, "generate_json")

    def test_generate_json_signature(self) -> None:
        """generate_json accepts prompt, system_prompt, and model params."""
        import inspect

        from gobby.llm.base import LLMProvider

        sig = inspect.signature(LLMProvider.generate_json)
        params = list(sig.parameters.keys())
        assert "prompt" in params
        assert "system_prompt" in params
        assert "model" in params


class TestLiteLLMGenerateJson:
    """Tests for LiteLLMProvider.generate_json()."""

    @pytest.mark.asyncio
    async def test_returns_parsed_dict(self, litellm_config: DaemonConfig) -> None:
        """generate_json() returns a parsed dict from the LLM response."""
        from gobby.llm.litellm import LiteLLMProvider

        provider = LiteLLMProvider(litellm_config)

        # Mock litellm.acompletion
        mock_response = MagicMock()
        mock_response.choices = [
            MagicMock(message=MagicMock(content='{"facts": ["fact1", "fact2"]}'))
        ]
        provider._litellm = MagicMock()
        provider._litellm.acompletion = AsyncMock(return_value=mock_response)

        result = await provider.generate_json("Extract facts from: test content")

        assert isinstance(result, dict)
        assert result == {"facts": ["fact1", "fact2"]}

    @pytest.mark.asyncio
    async def test_passes_response_format(self, litellm_config: DaemonConfig) -> None:
        """generate_json() passes response_format={'type':'json_object'} to acompletion."""
        from gobby.llm.litellm import LiteLLMProvider

        provider = LiteLLMProvider(litellm_config)

        mock_response = MagicMock()
        mock_response.choices = [MagicMock(message=MagicMock(content='{"result": "ok"}'))]
        provider._litellm = MagicMock()
        provider._litellm.acompletion = AsyncMock(return_value=mock_response)

        await provider.generate_json("test prompt")

        # Verify response_format was passed
        call_kwargs = provider._litellm.acompletion.call_args
        assert call_kwargs.kwargs.get("response_format") == {"type": "json_object"}

    @pytest.mark.asyncio
    async def test_malformed_json_raises_valueerror(self, litellm_config: DaemonConfig) -> None:
        """generate_json() raises ValueError on malformed JSON response."""
        from gobby.llm.litellm import LiteLLMProvider

        provider = LiteLLMProvider(litellm_config)

        mock_response = MagicMock()
        mock_response.choices = [MagicMock(message=MagicMock(content="This is not valid JSON"))]
        provider._litellm = MagicMock()
        provider._litellm.acompletion = AsyncMock(return_value=mock_response)

        with pytest.raises(ValueError, match="Failed to parse"):
            await provider.generate_json("test prompt")

    @pytest.mark.asyncio
    async def test_accepts_system_prompt(self, litellm_config: DaemonConfig) -> None:
        """generate_json() passes system_prompt to acompletion messages."""
        from gobby.llm.litellm import LiteLLMProvider

        provider = LiteLLMProvider(litellm_config)

        mock_response = MagicMock()
        mock_response.choices = [MagicMock(message=MagicMock(content='{"ok": true}'))]
        provider._litellm = MagicMock()
        provider._litellm.acompletion = AsyncMock(return_value=mock_response)

        await provider.generate_json("test prompt", system_prompt="You are a fact extractor.")

        call_kwargs = provider._litellm.acompletion.call_args
        messages = call_kwargs.kwargs.get("messages", [])
        system_msg = messages[0]
        assert system_msg["role"] == "system"
        assert system_msg["content"] == "You are a fact extractor."

    @pytest.mark.asyncio
    async def test_accepts_model_param(self, litellm_config: DaemonConfig) -> None:
        """generate_json() passes model override to acompletion."""
        from gobby.llm.litellm import LiteLLMProvider

        provider = LiteLLMProvider(litellm_config)

        mock_response = MagicMock()
        mock_response.choices = [MagicMock(message=MagicMock(content='{"ok": true}'))]
        provider._litellm = MagicMock()
        provider._litellm.acompletion = AsyncMock(return_value=mock_response)

        await provider.generate_json("test prompt", model="gpt-4o")

        call_kwargs = provider._litellm.acompletion.call_args
        assert call_kwargs.kwargs.get("model") == "gpt-4o"

    @pytest.mark.asyncio
    async def test_no_litellm_raises(self, litellm_config: DaemonConfig) -> None:
        """generate_json() raises RuntimeError when litellm not initialized."""
        from gobby.llm.litellm import LiteLLMProvider

        provider = LiteLLMProvider(litellm_config)
        provider._litellm = None

        with pytest.raises(RuntimeError, match="not initialized"):
            await provider.generate_json("test prompt")

    @pytest.mark.asyncio
    async def test_empty_content_raises(self, litellm_config: DaemonConfig) -> None:
        """generate_json() raises ValueError when response content is empty."""
        from gobby.llm.litellm import LiteLLMProvider

        provider = LiteLLMProvider(litellm_config)

        mock_response = MagicMock()
        mock_response.choices = [MagicMock(message=MagicMock(content=None))]
        provider._litellm = MagicMock()
        provider._litellm.acompletion = AsyncMock(return_value=mock_response)

        with pytest.raises(ValueError, match="Empty response"):
            await provider.generate_json("test prompt")


class TestClaudeGenerateJson:
    """Tests for ClaudeLLMProvider.generate_json() via LiteLLM fallback."""

    @pytest.mark.asyncio
    async def test_claude_api_key_mode_generate_json(self) -> None:
        """Claude in api_key mode uses LiteLLM for generate_json."""
        from gobby.llm.claude import ClaudeLLMProvider

        config = DaemonConfig(
            llm_providers=LLMProvidersConfig(
                claude=LLMProviderConfig(models="claude-haiku-4-5", auth_mode="api_key"),
                api_keys={"ANTHROPIC_API_KEY": "sk-test"},
            ),
        )
        provider = ClaudeLLMProvider(config, auth_mode="api_key")

        mock_response = MagicMock()
        mock_response.choices = [MagicMock(message=MagicMock(content='{"facts": ["test"]}'))]
        provider._litellm = MagicMock()
        provider._litellm.acompletion = AsyncMock(return_value=mock_response)

        result = await provider.generate_json("Extract facts")
        assert result == {"facts": ["test"]}

        # Verify anthropic/ prefix applied to model
        call_kwargs = provider._litellm.acompletion.call_args
        model = call_kwargs.kwargs.get("model", "")
        assert model.startswith("anthropic/")
