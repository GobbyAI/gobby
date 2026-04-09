"""Tests for provider routing in WebSocket chat sessions.

Covers:
- No provider -> SDK ChatSession (backward compat)
- provider="claude" -> CLIChatSession
- Session registers with correct source
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.servers.chat_session import ChatSession
from gobby.servers.websocket.chat._session import ChatSessionMixin

pytestmark = pytest.mark.unit


def _make_mixin(**overrides: Any) -> ChatSessionMixin:
    """Create a ChatSessionMixin instance with test defaults."""
    mixin = ChatSessionMixin.__new__(ChatSessionMixin)
    mixin.clients = {}
    mixin._chat_sessions = {}
    mixin._active_chat_tasks = {}
    mixin._pending_modes = {}
    mixin._pending_worktree_paths = {}
    mixin._pending_agents = {}
    mixin._pending_projects = {}
    mixin._pending_providers = {}
    mixin._session_create_locks = {}
    mixin._fire_lifecycle = AsyncMock(return_value=None)
    mixin._inject_pending_messages = MagicMock(return_value=None)
    mixin.broadcast_session_event = AsyncMock(return_value=None)
    mixin.hook_broadcaster = None
    mixin.session_manager = MagicMock()
    mixin.session_manager.db = MagicMock()
    mixin.session_manager.register = MagicMock(
        return_value=MagicMock(
            id="db-session-1",
            seq_num=12,
            usage_output_tokens=0,
            chat_mode="code",
            approved_tools_json=None,
        )
    )
    for k, v in overrides.items():
        setattr(mixin, k, v)
    return mixin


class TestDefaultProvider:
    """Tests for default provider (no provider specified)."""

    @pytest.mark.asyncio
    async def test_no_provider_creates_sdk_chat_session(self) -> None:
        """When no provider is specified, should create SDK ChatSession (backward compat)."""
        mixin = _make_mixin()

        # The session creation function should default to ChatSession
        # when no provider is specified in the request
        session = await _create_session_for_provider(mixin, provider=None)

        assert isinstance(session, ChatSession)

    @pytest.mark.asyncio
    async def test_no_provider_backward_compatible(self) -> None:
        """Existing clients that don't send provider should still work."""
        mixin = _make_mixin()

        session = await _create_session_for_provider(mixin, provider=None)

        # Should be a standard SDK ChatSession.
        assert isinstance(session, ChatSession)


class TestClaudeProvider:
    """Tests for provider='claude'."""

    @pytest.mark.asyncio
    async def test_claude_provider_creates_sdk_chat_session(self) -> None:
        """provider='claude' should create the Claude SDK ChatSession."""
        mixin = _make_mixin()

        session = await _create_session_for_provider(mixin, provider="claude")

        assert isinstance(session, ChatSession)

    @pytest.mark.asyncio
    async def test_claude_provider_has_correct_provider_attr(self) -> None:
        """ChatSession created via provider='claude' should have provider='claude'."""
        mixin = _make_mixin()

        session = await _create_session_for_provider(mixin, provider="claude")

        assert session.provider == "claude"


class TestPendingProviderOverride:
    """Tests for queued UI provider overrides."""

    @pytest.mark.asyncio
    async def test_pending_provider_takes_precedence_over_agent_or_default(self) -> None:
        """Queued provider overrides should route the next session creation."""
        mixin = _make_mixin()
        mixin._pending_providers["test-conv-routing"] = "gemini"

        session = await _create_session_for_provider(mixin, provider=None)

        assert session.provider == "gemini"


class TestSessionRegistration:
    """Tests for session source registration."""

    @pytest.mark.asyncio
    async def test_sdk_session_registers_with_sdk_source(self) -> None:
        """SDK ChatSession should register with source='web_chat' or 'sdk'."""
        mixin = _make_mixin()

        session = await _create_session_for_provider(mixin, provider=None)

        # The session should have been registered in the DB with appropriate source
        assert session is not None
        assert mixin._chat_sessions["test-conv-routing"] is session
        register_kwargs = mixin.session_manager.register.call_args.kwargs
        assert register_kwargs["source"] == "claude"
        assert register_kwargs["session_type"] == "web_chat"

    @pytest.mark.asyncio
    async def test_cli_session_registers_with_cli_source(self) -> None:
        """Explicit Claude provider should still register the session in the mixin and DB."""
        mixin = _make_mixin()

        session = await _create_session_for_provider(mixin, provider="claude")

        assert session is not None
        assert mixin._chat_sessions["test-conv-routing"] is session
        register_kwargs = mixin.session_manager.register.call_args.kwargs
        assert register_kwargs["source"] == "claude"
        assert register_kwargs["session_type"] == "web_chat"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _create_session_for_provider(
    mixin: ChatSessionMixin,
    provider: str | None,
) -> Any:
    """Helper to invoke provider routing logic and return the created session.

    This calls into the session creation path that routes based on provider.
    The exact method name may differ in implementation — this helper isolates
    that coupling.
    """
    with (
        patch(
            "gobby.servers.chat_session.ChatSession.start",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "gobby.servers.gemini_cli_chat_session.GeminiCLIChatSession.start",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "gobby.servers.codex_cli_chat_session.CodexCLIChatSession.start",
            new=AsyncMock(return_value=None),
        ),
    ):
        return await mixin._create_chat_session(
            conversation_id="test-conv-routing",
            provider=provider,
        )
