"""Tests for hold-open gate behavior on tool approval.

Covers:
- Terminal sessions go through existing adapter path (no hold-open)
- Web chat PreToolUse on gated tool creates pending interaction
- Approve -> hook returns approve decision
- Timeout -> auto-deny
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from gobby.servers.routes.hold_open_gate import (
    HoldOpenGate,
    is_hold_open_session,
)

pytestmark = pytest.mark.unit


@pytest.fixture
def mock_interaction_manager() -> AsyncMock:
    """Create a mock PendingInteractionManager."""
    manager = AsyncMock()
    manager.create = AsyncMock(return_value="interaction-1")
    manager.wait = AsyncMock(return_value={"decision": "approve", "response": {}})
    manager.resolve = AsyncMock()
    manager.expire = AsyncMock()
    manager.supersede = AsyncMock()
    return manager


@pytest.fixture
def gate(mock_interaction_manager: AsyncMock) -> HoldOpenGate:
    """Create a HoldOpenGate with mock dependencies."""
    return HoldOpenGate(interaction_manager=mock_interaction_manager)


class TestTerminalSessionBypass:
    """Terminal sessions should NOT go through hold-open gate."""

    def test_terminal_session_is_not_hold_open(self) -> None:
        """Terminal/adapter sessions should not be identified as hold-open."""
        # Terminal sessions come through the adapter path (hooks)
        assert not is_hold_open_session(source="terminal")

    def test_adapter_session_is_not_hold_open(self) -> None:
        """Adapter-originated sessions should not use hold-open."""
        assert not is_hold_open_session(source="claude_code")

    def test_web_chat_session_is_hold_open(self) -> None:
        """Web chat sessions should be identified as hold-open capable."""
        assert is_hold_open_session(source="web_chat")


class TestWebChatPreToolUse:
    """Web chat PreToolUse on gated tool creates pending interaction."""

    @pytest.mark.asyncio
    async def test_gated_tool_creates_pending_interaction(
        self,
        gate: HoldOpenGate,
        mock_interaction_manager: AsyncMock,
    ) -> None:
        """PreToolUse on a gated tool should create a pending interaction."""
        hook_event = {
            "type": "PreToolUse",
            "session_id": "sess-web-1",
            "tool_name": "bash",
            "tool_input": {"command": "rm -rf /"},
            "source": "web_chat",
        }

        # This should create a pending interaction and wait
        result = await gate.handle_pre_tool_use(hook_event)

        mock_interaction_manager.create.assert_awaited_once()
        assert result is not None

    @pytest.mark.asyncio
    async def test_non_gated_tool_passes_through(
        self,
        gate: HoldOpenGate,
        mock_interaction_manager: AsyncMock,
    ) -> None:
        """PreToolUse on a non-gated tool should pass through without hold-open."""
        hook_event = {
            "type": "PreToolUse",
            "session_id": "sess-web-1",
            "tool_name": "mcp__gobby__list_tasks",
            "tool_input": {},
            "source": "web_chat",
        }

        await gate.handle_pre_tool_use(hook_event)

        # Non-gated tools should not create a pending interaction
        mock_interaction_manager.create.assert_not_awaited()


class TestApproveDecision:
    """Approve decision flows back to the hook."""

    @pytest.mark.asyncio
    async def test_approve_returns_approve_decision(
        self,
        gate: HoldOpenGate,
        mock_interaction_manager: AsyncMock,
    ) -> None:
        """When user approves, hook should return an approve decision."""
        mock_interaction_manager.wait.return_value = {
            "decision": "approve",
            "response": {},
        }

        hook_event = {
            "type": "PreToolUse",
            "session_id": "sess-web-1",
            "tool_name": "bash",
            "tool_input": {"command": "ls"},
            "source": "web_chat",
        }

        result = await gate.handle_pre_tool_use(hook_event)

        assert result is not None
        assert result.get("decision") == "approve" or result.get("allow") is True

    @pytest.mark.asyncio
    async def test_deny_returns_deny_decision(
        self,
        gate: HoldOpenGate,
        mock_interaction_manager: AsyncMock,
    ) -> None:
        """When user denies, hook should return a deny decision."""
        mock_interaction_manager.wait.return_value = {
            "decision": "deny",
            "response": {"reason": "User declined"},
        }

        hook_event = {
            "type": "PreToolUse",
            "session_id": "sess-web-1",
            "tool_name": "bash",
            "tool_input": {"command": "dangerous"},
            "source": "web_chat",
        }

        result = await gate.handle_pre_tool_use(hook_event)

        assert result is not None
        assert result.get("decision") == "deny" or result.get("allow") is False


class TestTimeout:
    """Timeout behavior auto-denies."""

    @pytest.mark.asyncio
    async def test_timeout_auto_denies(
        self,
        gate: HoldOpenGate,
        mock_interaction_manager: AsyncMock,
    ) -> None:
        """When interaction times out, should auto-deny the tool use."""
        mock_interaction_manager.wait.return_value = {
            "decision": "expired",
            "response": {},
        }

        hook_event = {
            "type": "PreToolUse",
            "session_id": "sess-web-1",
            "tool_name": "bash",
            "tool_input": {"command": "slow"},
            "source": "web_chat",
        }

        result = await gate.handle_pre_tool_use(hook_event)

        # Expired/timeout should result in deny
        assert result is not None
        assert result.get("decision") == "deny" or result.get("allow") is False
