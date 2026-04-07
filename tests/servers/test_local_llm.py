"""Tests for local LLM support via ANTHROPIC_BASE_URL."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.config.app import DaemonConfig, LocalLLMConfig

pytestmark = pytest.mark.unit


class TestLocalLLMConfig:
    """Tests for LocalLLMConfig model."""

    def test_defaults(self) -> None:
        """LocalLLMConfig defaults to disabled with empty endpoint."""
        cfg = LocalLLMConfig()
        assert cfg.enabled is False
        assert cfg.endpoint == ""
        assert cfg.providers == ["claude"]

    def test_enabled_with_endpoint(self) -> None:
        """LocalLLMConfig can be enabled with a custom endpoint."""
        cfg = LocalLLMConfig(
            enabled=True,
            endpoint="http://localhost:1234/v1",
            providers=["claude", "gemini"],
        )
        assert cfg.enabled is True
        assert cfg.endpoint == "http://localhost:1234/v1"
        assert cfg.providers == ["claude", "gemini"]

    def test_daemon_config_has_local_llm(self) -> None:
        """DaemonConfig includes local_llm field with defaults."""
        config = DaemonConfig()
        assert hasattr(config, "local_llm")
        assert isinstance(config.local_llm, LocalLLMConfig)
        assert config.local_llm.enabled is False


class TestChatSessionLocalLLM:
    """Tests for ANTHROPIC_BASE_URL injection in ChatSession.start()."""

    @pytest.mark.asyncio
    async def test_env_includes_base_url_when_local_llm_enabled(self) -> None:
        """ChatSession.start() sets ANTHROPIC_BASE_URL when local_llm is enabled."""
        from gobby.servers.chat_session import ChatSession

        session = ChatSession(conversation_id="test-conv")
        config = MagicMock()
        config.local_llm = LocalLLMConfig(
            enabled=True,
            endpoint="http://localhost:8080/v1",
            providers=["claude"],
        )
        session._config = config

        # Mock out the actual SDK client creation
        with (
            patch("gobby.servers.chat_session._find_cli_path", return_value="/usr/bin/claude"),
            patch("gobby.servers.chat_session._find_project_root", return_value=None),
            patch("gobby.servers.chat_session._load_chat_system_prompt", return_value="test"),
            patch("gobby.servers.chat_session._build_gobby_mcp_entry", return_value={}),
            patch("gobby.servers.chat_session.ClaudeSDKClient") as mock_sdk,
        ):
            mock_client = AsyncMock()
            mock_sdk.return_value = mock_client

            await session.start(model="opus")

            # Verify the ClaudeSDKClient was created with ANTHROPIC_BASE_URL in env
            call_kwargs = mock_sdk.call_args
            options = call_kwargs.kwargs.get("options") or call_kwargs.args[0]
            assert options.env.get("ANTHROPIC_BASE_URL") == "http://localhost:8080/v1"

    @pytest.mark.asyncio
    async def test_env_omits_base_url_when_local_llm_disabled(self) -> None:
        """ChatSession.start() does NOT set ANTHROPIC_BASE_URL when local_llm is disabled."""
        from gobby.servers.chat_session import ChatSession

        session = ChatSession(conversation_id="test-conv-2")
        config = MagicMock()
        config.local_llm = LocalLLMConfig(enabled=False, endpoint="http://localhost:8080/v1")
        session._config = config

        with (
            patch("gobby.servers.chat_session._find_cli_path", return_value="/usr/bin/claude"),
            patch("gobby.servers.chat_session._find_project_root", return_value=None),
            patch("gobby.servers.chat_session._load_chat_system_prompt", return_value="test"),
            patch("gobby.servers.chat_session._build_gobby_mcp_entry", return_value={}),
            patch("gobby.servers.chat_session.ClaudeSDKClient") as mock_sdk,
        ):
            mock_client = AsyncMock()
            mock_sdk.return_value = mock_client

            await session.start(model="opus")

            call_kwargs = mock_sdk.call_args
            options = call_kwargs.kwargs.get("options") or call_kwargs.args[0]
            assert "ANTHROPIC_BASE_URL" not in options.env

    @pytest.mark.asyncio
    async def test_env_omits_base_url_when_provider_not_claude(self) -> None:
        """ChatSession.start() does NOT set ANTHROPIC_BASE_URL when claude not in providers."""
        from gobby.servers.chat_session import ChatSession

        session = ChatSession(conversation_id="test-conv-3")
        config = MagicMock()
        config.local_llm = LocalLLMConfig(
            enabled=True,
            endpoint="http://localhost:8080/v1",
            providers=["gemini"],  # Not claude
        )
        session._config = config

        with (
            patch("gobby.servers.chat_session._find_cli_path", return_value="/usr/bin/claude"),
            patch("gobby.servers.chat_session._find_project_root", return_value=None),
            patch("gobby.servers.chat_session._load_chat_system_prompt", return_value="test"),
            patch("gobby.servers.chat_session._build_gobby_mcp_entry", return_value={}),
            patch("gobby.servers.chat_session.ClaudeSDKClient") as mock_sdk,
        ):
            mock_client = AsyncMock()
            mock_sdk.return_value = mock_client

            await session.start(model="opus")

            call_kwargs = mock_sdk.call_args
            options = call_kwargs.kwargs.get("options") or call_kwargs.args[0]
            assert "ANTHROPIC_BASE_URL" not in options.env
