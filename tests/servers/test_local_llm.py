"""Tests for local generation configuration."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import ValidationError

from gobby.config.ai import LocalGenerationConfig
from gobby.config.app import DaemonConfig, LocalConfig

pytestmark = pytest.mark.unit


class TestLocalGenerationConfig:
    """Tests for ai.generation.local config."""

    def test_defaults(self) -> None:
        cfg = LocalGenerationConfig()

        assert cfg.enabled is False
        assert cfg.api_base is None
        assert cfg.model is None
        assert cfg.api_key is None

    def test_enabled_with_endpoint_and_model(self) -> None:
        cfg = LocalGenerationConfig(
            enabled=True,
            api_base="http://localhost:1234/v1",
            model="qwen-coder",
            api_key="local-key",
        )

        assert cfg.enabled is True
        assert cfg.api_base == "http://localhost:1234/v1"
        assert cfg.model == "qwen-coder"
        assert cfg.api_key == "local-key"

    def test_enabled_requires_endpoint(self) -> None:
        with pytest.raises(ValidationError, match="api_base"):
            LocalGenerationConfig(enabled=True, model="qwen-coder")

    def test_enabled_requires_model(self) -> None:
        with pytest.raises(ValidationError, match="model"):
            LocalGenerationConfig(enabled=True, api_base="http://localhost:1234/v1")

    def test_daemon_config_has_ai_generation_local(self) -> None:
        config = DaemonConfig()

        assert config.ai.generation.local.enabled is False

    def test_daemon_config_rejects_removed_local_llm(self) -> None:
        with pytest.raises(ValidationError, match="local_llm config has been removed"):
            DaemonConfig(local_llm={"enabled": True, "endpoint": "http://localhost:1234/v1"})


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
