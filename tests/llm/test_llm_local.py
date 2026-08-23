"""Tests for the LocalLLMProvider."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from openai import BadRequestError

from gobby.config.app import DaemonConfig
from gobby.llm.base import LLMProviderError
from gobby.llm.local import _CLOUD_MODEL_ALIASES, LocalLLMProvider

pytestmark = pytest.mark.unit


def _assert_bounded_openai_client(mock_cls: MagicMock, *, api_key: str) -> None:
    mock_cls.assert_called_once()
    kwargs = mock_cls.call_args.kwargs
    assert kwargs["base_url"] == "http://localhost:1234/v1"
    assert kwargs["api_key"] == api_key
    assert kwargs["max_retries"] == 0
    timeout = kwargs["timeout"]
    assert isinstance(timeout, httpx.Timeout)
    assert timeout.connect == 5.0
    assert timeout.read == 120.0
    assert timeout.write == 30.0
    assert timeout.pool == 5.0


# ─── Fixtures ───


@pytest.fixture
def daemon_config() -> DaemonConfig:
    return DaemonConfig(
        ai={
            "generation": {
                "endpoints": {
                    "lm-studio": {
                        "api_base": "http://localhost:1234/v1",
                        "model": "qwen2.5-coder-7b",
                    }
                }
            }
        }
    )


@pytest.fixture
def provider(daemon_config: DaemonConfig) -> LocalLLMProvider:
    with patch("openai.AsyncOpenAI") as mock_cls:
        mock_cls.return_value = MagicMock()
        p = LocalLLMProvider(daemon_config, endpoint_name="lm-studio")
    return p


# ═══════════════════════════════════════════════════════════════════════
# Initialisation
# ═══════════════════════════════════════════════════════════════════════


class TestLocalLLMProviderInit:
    def test_init_with_valid_config(self, daemon_config: DaemonConfig) -> None:
        with patch("openai.AsyncOpenAI"):
            p = LocalLLMProvider(daemon_config, endpoint_name="lm-studio")
        assert p.provider_name == "endpoint:lm-studio"
        assert p.auth_mode == "api_key"
        assert p._default_model == "qwen2.5-coder-7b"
        assert p._url == "http://localhost:1234/v1"

    def test_init_with_named_generation_endpoint(self) -> None:
        config = DaemonConfig(
            ai={
                "generation": {
                    "endpoints": {
                        "lm-studio": {
                            "api_base": "http://localhost:1234/v1",
                            "model": "qwen-coder",
                            "api_key": "test-key",
                        },
                        "ollama": {
                            "api_base": "http://localhost:11434/v1",
                            "model": "qwen2.5-coder",
                        },
                    }
                }
            }
        )

        with patch("openai.AsyncOpenAI") as mock_cls:
            p = LocalLLMProvider(config, endpoint_name="lm-studio")

        assert p.provider_name == "endpoint:lm-studio"
        assert p._default_model == "qwen-coder"
        assert p._url == "http://localhost:1234/v1"
        _assert_bounded_openai_client(mock_cls, api_key="test-key")

    def test_unknown_named_generation_endpoint_raises(self) -> None:
        with pytest.raises(ValueError, match="Unknown generation endpoint"):
            LocalLLMProvider(DaemonConfig(), endpoint_name="lm-studio")

    def test_init_without_endpoint_name_raises(self, daemon_config: DaemonConfig) -> None:
        with pytest.raises(ValueError, match="named local generation endpoint"):
            LocalLLMProvider(daemon_config, endpoint_name="")

    def test_init_without_matching_endpoint_raises(self) -> None:
        with pytest.raises(ValueError, match="Unknown generation endpoint"):
            LocalLLMProvider(DaemonConfig(), endpoint_name="lm-studio")

    def test_keyless_endpoint_sends_empty_api_key(self, daemon_config: DaemonConfig) -> None:
        with patch("openai.AsyncOpenAI") as mock_cls:
            LocalLLMProvider(daemon_config, endpoint_name="lm-studio")
        # An empty key makes the SDK omit the Authorization header entirely.
        _assert_bounded_openai_client(mock_cls, api_key="")

    def test_api_key_passthrough(self) -> None:
        config = DaemonConfig(
            ai={
                "generation": {
                    "endpoints": {
                        "lm-studio": {
                            "api_base": "http://localhost:1234/v1",
                            "model": "test",
                            "api_key": "my-secret-key",
                        }
                    }
                }
            }
        )
        with patch("openai.AsyncOpenAI") as mock_cls:
            LocalLLMProvider(config, endpoint_name="lm-studio")
        _assert_bounded_openai_client(mock_cls, api_key="my-secret-key")


# ═══════════════════════════════════════════════════════════════════════
# Model resolution
# ═══════════════════════════════════════════════════════════════════════


class TestModelResolution:
    def test_none_returns_default(self, provider: LocalLLMProvider) -> None:
        assert provider._resolve_model(None) == "qwen2.5-coder-7b"

    def test_explicit_model_passthrough(self, provider: LocalLLMProvider) -> None:
        assert provider._resolve_model("llama3.1-8b") == "llama3.1-8b"

    def test_cloud_alias_warns_and_falls_back(self, provider: LocalLLMProvider) -> None:
        for alias in ("haiku", "sonnet", "opus", "fable", "gpt-4o", "o3-mini"):
            assert alias.lower() in _CLOUD_MODEL_ALIASES
            result = provider._resolve_model(alias)
            assert result == "qwen2.5-coder-7b"

    def test_cloud_alias_case_insensitive(self, provider: LocalLLMProvider) -> None:
        assert provider._resolve_model("Haiku") == "qwen2.5-coder-7b"
        assert provider._resolve_model("SONNET") == "qwen2.5-coder-7b"
        assert provider._resolve_model("FABLE") == "qwen2.5-coder-7b"


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
    async def test_result_includes_usage_when_available(self, provider: LocalLLMProvider) -> None:
        mock_response = MagicMock()
        mock_response.choices = [MagicMock()]
        mock_response.choices[0].message.content = "Fix Auth Bug"
        mock_response.usage = {
            "prompt_tokens": 12,
            "completion_tokens": 4,
            "total_tokens": 16,
        }
        provider._client.chat.completions.create = AsyncMock(return_value=mock_response)

        result = await provider.generate_text_result("Fix the auth bug")

        assert result.text == "Fix Auth Bug"
        assert result.usage == {
            "prompt_tokens": 12,
            "completion_tokens": 4,
            "total_tokens": 16,
        }

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
        provider._adapter._client = None
        with pytest.raises(RuntimeError, match="not initialised"):
            await provider.generate_text("hello")

    @pytest.mark.asyncio
    async def test_empty_response_raises_provider_error(self, provider: LocalLLMProvider) -> None:
        mock_response = MagicMock()
        mock_response.choices = [MagicMock()]
        mock_response.choices[0].message.content = None
        provider._client.chat.completions.create = AsyncMock(return_value=mock_response)

        with pytest.raises(LLMProviderError, match="qwen2.5-coder-7b.*blank content"):
            await provider.generate_text("hello")


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
        json_mode_error = BadRequestError(
            message="response_format json_object not supported",
            response=httpx.Response(
                400,
                request=httpx.Request("POST", "http://test"),
            ),
            body=None,
        )

        call_count = 0

        async def side_effect(**kwargs: object) -> MagicMock:
            nonlocal call_count
            call_count += 1
            if call_count == 1 and "response_format" in kwargs:
                raise json_mode_error
            return mock_response

        provider._client.chat.completions.create = AsyncMock(side_effect=side_effect)

        result = await provider.generate_json("Give me JSON")
        assert result == {"ok": True}
        assert call_count == 2
        second_request = provider._client.chat.completions.create.call_args_list[1].kwargs
        assert "response_format" not in second_request

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
