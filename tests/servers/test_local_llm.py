"""Tests for local generation configuration."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import ValidationError

from gobby.config.ai import LocalGenerationConfig, LocalGenerationEndpointConfig
from gobby.config.app import DaemonConfig, LocalConfig

pytestmark = pytest.mark.unit


class TestLocalGenerationConfig:
    """Tests for ai.generation.local config."""

    def test_defaults(self) -> None:
        cfg = LocalGenerationConfig()

        assert cfg.endpoints == {}

    def test_endpoint_with_api_base_and_model(self) -> None:
        cfg = LocalGenerationConfig(
            endpoints={
                "lm-studio": LocalGenerationEndpointConfig(
                    api_base="http://localhost:1234/v1",
                    model="qwen-coder",
                    api_key="local-key",
                ),
                "ollama": {
                    "api_base": "http://localhost:11434/v1",
                    "model": "qwen2.5-coder",
                },
            }
        )

        assert cfg.endpoints["lm-studio"].api_base == "http://localhost:1234/v1"
        assert cfg.endpoints["lm-studio"].model == "qwen-coder"
        assert cfg.endpoints["lm-studio"].api_key == "local-key"
        assert cfg.endpoints["ollama"].model == "qwen2.5-coder"

    def test_endpoint_requires_api_base(self) -> None:
        with pytest.raises(ValidationError, match="api_base"):
            LocalGenerationEndpointConfig(api_base="", model="qwen-coder")

    def test_endpoint_requires_model(self) -> None:
        with pytest.raises(ValidationError, match="model"):
            LocalGenerationEndpointConfig(api_base="http://localhost:1234/v1", model="")

    @pytest.mark.parametrize("name", ["", "lm/studio", "lm:studio", "LmStudio"])
    def test_endpoint_names_must_be_lowercase_slugs(self, name: str) -> None:
        with pytest.raises(ValidationError, match="endpoint names"):
            LocalGenerationConfig(
                endpoints={
                    name: {
                        "api_base": "http://localhost:1234/v1",
                        "model": "qwen-coder",
                    }
                }
            )

    def test_daemon_config_has_ai_generation_local(self) -> None:
        config = DaemonConfig()

        assert config.ai.generation.local.endpoints == {}


class TestChatSessionLocalModel:
    """Tests for explicit model='local' routing in ChatSession.start()."""

    @pytest.mark.asyncio
    async def test_model_local_uses_configured_local_endpoint(self) -> None:
        from gobby.servers.chat_session import ChatSession

        session = ChatSession(conversation_id="test-local-model")
        config = MagicMock()
        config.local = LocalConfig(
            url="http://localhost:1234/v1",
            model="qwen-coder-32b",
            api_key="test-local-key",
        )
        session._config = config

        with (
            patch("gobby.servers.chat_session._find_cli_path", return_value="/usr/bin/claude"),
            patch("gobby.servers.chat_session._find_project_root", return_value=None),
            patch("gobby.servers.chat_session._load_chat_system_prompt", return_value="test"),
            patch("gobby.servers.chat_session._build_gobby_mcp_entry", return_value={}),
            patch(
                "gobby.agents.local_model.ensure_local_model",
                new=AsyncMock(return_value="qwen-coder-32b"),
            ),
            patch("gobby.servers.chat_session.ClaudeSDKClient") as mock_sdk,
        ):
            mock_client = AsyncMock()
            mock_sdk.return_value = mock_client

            await session.start(model="local")

            call_kwargs = mock_sdk.call_args
            options = call_kwargs.kwargs.get("options") or call_kwargs.args[0]
            assert options.model == "qwen-coder-32b"
            assert options.env.get("ANTHROPIC_BASE_URL") == "http://localhost:1234/v1"
            assert options.env.get("ANTHROPIC_AUTH_TOKEN") == "test-local-key"
            assert session.model == "local"
