"""Tests for the LocalLLMProvider."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.config.app import DaemonConfig, LocalConfig
from gobby.config.llm_providers import LLMProviderConfig, LLMProvidersConfig
from gobby.llm.local import _CLOUD_MODEL_ALIASES, LocalLLMProvider

pytestmark = pytest.mark.unit


# ─── Fixtures ───


@pytest.fixture
def local_config() -> LocalConfig:
    return LocalConfig(url="http://localhost:1234/v1", model="qwen2.5-coder-7b")


@pytest.fixture
def daemon_config(local_config: LocalConfig) -> DaemonConfig:
    return DaemonConfig(
        llm_providers=LLMProvidersConfig(
            claude=LLMProviderConfig(models="haiku,sonnet,opus"),
        ),
        local=local_config,
    )


@pytest.fixture
def provider(daemon_config: DaemonConfig) -> LocalLLMProvider:
    with patch("openai.AsyncOpenAI") as mock_cls:
        mock_cls.return_value = MagicMock()
        p = LocalLLMProvider(daemon_config)
    return p


# ═══════════════════════════════════════════════════════════════════════
# Initialisation
# ═══════════════════════════════════════════════════════════════════════


class TestLocalLLMProviderInit:
    def test_init_with_valid_config(self, daemon_config: DaemonConfig) -> None:
        with patch("openai.AsyncOpenAI"):
            p = LocalLLMProvider(daemon_config)
        assert p.provider_name == "local"
        assert p.auth_mode == "api_key"
        assert p._default_model == "qwen2.5-coder-7b"
        assert p._url == "http://localhost:1234/v1"

    def test_init_without_local_config_raises(self) -> None:
        config = DaemonConfig(
            llm_providers=LLMProvidersConfig(
                claude=LLMProviderConfig(models="haiku"),
            ),
            local=None,
        )
        with pytest.raises(ValueError, match="local"):
            LocalLLMProvider(config)

    def test_api_key_defaults_to_not_needed(self, daemon_config: DaemonConfig) -> None:
        with patch("openai.AsyncOpenAI") as mock_cls:
            LocalLLMProvider(daemon_config)
        mock_cls.assert_called_once_with(
            base_url="http://localhost:1234/v1",
            api_key="not-needed",
        )
        assert mock_cls.call_count == 1
        assert mock_cls.call_args is not None

    def test_api_key_passthrough(self) -> None:
        config = DaemonConfig(
            llm_providers=LLMProvidersConfig(
                claude=LLMProviderConfig(models="haiku"),
            ),
            local=LocalConfig(
                url="http://localhost:1234/v1",
                model="test",
                api_key="my-secret-key",
            ),
        )
        with patch("openai.AsyncOpenAI") as mock_cls:
            LocalLLMProvider(config)
        mock_cls.assert_called_once_with(
            base_url="http://localhost:1234/v1",
            api_key="my-secret-key",
        )
        assert mock_cls.call_count == 1
        assert mock_cls.call_args is not None


# ═══════════════════════════════════════════════════════════════════════
# Model resolution
# ═══════════════════════════════════════════════════════════════════════


class TestModelResolution:
    def test_none_returns_default(self, provider: LocalLLMProvider) -> None:
        assert provider._resolve_model(None) == "qwen2.5-coder-7b"

    def test_explicit_model_passthrough(self, provider: LocalLLMProvider) -> None:
        assert provider._resolve_model("llama3.1-8b") == "llama3.1-8b"

    def test_cloud_alias_warns_and_falls_back(self, provider: LocalLLMProvider) -> None:
        for alias in ("haiku", "sonnet", "opus", "gpt-4o", "o3-mini"):
            assert alias.lower() in _CLOUD_MODEL_ALIASES
            result = provider._resolve_model(alias)
            assert result == "qwen2.5-coder-7b"

    def test_cloud_alias_case_insensitive(self, provider: LocalLLMProvider) -> None:
        assert provider._resolve_model("Haiku") == "qwen2.5-coder-7b"
        assert provider._resolve_model("SONNET") == "qwen2.5-coder-7b"


# ═══════════════════════════════════════════════════════════════════════
# generate_text
# ═══════════════════════════════════════════════════════════════════════


class TestGenerateText:
    @pytest.mark.asyncio
    async def test_success(self, provider: LocalLLMProvider) -> None:
        mock_response = MagicMock()
        mock_response.choices = [MagicMock()]
        mock_response.choices[0].message.content = "Fix Auth Bug"
        provider._client.chat.completions.create = AsyncMock(return_value=mock_response)

        result = await provider.generate_text("Fix the auth bug", system_prompt="Be helpful")
        assert result == "Fix Auth Bug"

        provider._client.chat.completions.create.assert_awaited_once()
        call_kwargs = provider._client.chat.completions.create.call_args.kwargs
        assert call_kwargs["model"] == "qwen2.5-coder-7b"

    @pytest.mark.asyncio
    async def test_model_override(self, provider: LocalLLMProvider) -> None:
        mock_response = MagicMock()
        mock_response.choices = [MagicMock()]
        mock_response.choices[0].message.content = "Title"
        provider._client.chat.completions.create = AsyncMock(return_value=mock_response)

        await provider.generate_text("prompt", model="custom-model-7b")
        call_kwargs = provider._client.chat.completions.create.call_args.kwargs
        assert call_kwargs["model"] == "custom-model-7b"

    @pytest.mark.asyncio
    async def test_cloud_alias_uses_default(self, provider: LocalLLMProvider) -> None:
        mock_response = MagicMock()
        mock_response.choices = [MagicMock()]
        mock_response.choices[0].message.content = "Title"
        provider._client.chat.completions.create = AsyncMock(return_value=mock_response)

        await provider.generate_text("prompt", model="haiku")
        call_kwargs = provider._client.chat.completions.create.call_args.kwargs
        assert call_kwargs["model"] == "qwen2.5-coder-7b"

    @pytest.mark.asyncio
    async def test_no_client_raises(self, provider: LocalLLMProvider) -> None:
        provider._client = None
        with pytest.raises(RuntimeError, match="not initialised"):
            await provider.generate_text("hello")

    @pytest.mark.asyncio
    async def test_empty_response_returns_empty(self, provider: LocalLLMProvider) -> None:
        mock_response = MagicMock()
        mock_response.choices = [MagicMock()]
        mock_response.choices[0].message.content = None
        provider._client.chat.completions.create = AsyncMock(return_value=mock_response)

        result = await provider.generate_text("hello")
        assert result == ""


# ═══════════════════════════════════════════════════════════════════════
# generate_json
# ═══════════════════════════════════════════════════════════════════════


class TestGenerateJson:
    @pytest.mark.asyncio
    async def test_success(self, provider: LocalLLMProvider) -> None:
        mock_response = MagicMock()
        mock_response.choices = [MagicMock()]
        mock_response.choices[0].message.content = '{"key": "value"}'
        provider._client.chat.completions.create = AsyncMock(return_value=mock_response)

        result = await provider.generate_json("Give me JSON")
        assert result == {"key": "value"}

    @pytest.mark.asyncio
    async def test_strips_markdown_fences(self, provider: LocalLLMProvider) -> None:
        mock_response = MagicMock()
        mock_response.choices = [MagicMock()]
        mock_response.choices[0].message.content = '```json\n{"key": "value"}\n```'
        provider._client.chat.completions.create = AsyncMock(return_value=mock_response)

        result = await provider.generate_json("Give me JSON")
        assert result == {"key": "value"}

    @pytest.mark.asyncio
    async def test_fallback_without_response_format(self, provider: LocalLLMProvider) -> None:
        """When json_object mode is rejected, retries without response_format."""
        mock_response = MagicMock()
        mock_response.choices = [MagicMock()]
        mock_response.choices[0].message.content = '{"ok": true}'

        call_count = 0

        async def side_effect(**kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1 and "response_format" in kwargs:
                raise Exception("response_format not supported")
            return mock_response

        provider._client.chat.completions.create = AsyncMock(side_effect=side_effect)

        result = await provider.generate_json("Give me JSON")
        assert result == {"ok": True}
        assert call_count == 2

    @pytest.mark.asyncio
    async def test_invalid_json_raises(self, provider: LocalLLMProvider) -> None:
        mock_response = MagicMock()
        mock_response.choices = [MagicMock()]
        mock_response.choices[0].message.content = "not json at all"
        provider._client.chat.completions.create = AsyncMock(return_value=mock_response)

        with pytest.raises(ValueError, match="Failed to parse"):
            await provider.generate_json("Give me JSON")

    @pytest.mark.asyncio
    async def test_empty_response_raises(self, provider: LocalLLMProvider) -> None:
        mock_response = MagicMock()
        mock_response.choices = [MagicMock()]
        mock_response.choices[0].message.content = None
        provider._client.chat.completions.create = AsyncMock(return_value=mock_response)

        with pytest.raises(ValueError, match="Empty response"):
            await provider.generate_json("Give me JSON")
