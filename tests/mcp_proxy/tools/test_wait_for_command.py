"""Tests for the wait_for_command tool in agent_messaging.

Covers:
- Immediate return when command already pending
- Polls until command found
- Timeout when no command arrives
- Auto-activate sets status and session variables
- No auto-activate leaves command pending
- Poll interval floor (negative/zero defaults to 5s)
"""

from __future__ import annotations

from dataclasses import dataclass
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.mcp_proxy.tools.internal import InternalToolRegistry

pytestmark = pytest.mark.unit


# ═══════════════════════════════════════════════════════════════════════
# Mock helpers
# ═══════════════════════════════════════════════════════════════════════


@dataclass
class MockCommand:
    id: str = "cmd-1"
    from_session: str = "s-parent"
    to_session: str = "s-child"
    command_text: str = "Run tests"
    status: str = "pending"
    created_at: str = "2026-01-01T00:00:00"
    allowed_tools: str | None = None
    allowed_mcp_tools: str | None = None
    exit_condition: str | None = None
    started_at: str | None = None
    completed_at: str | None = None

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "from_session": self.from_session,
            "to_session": self.to_session,
            "command_text": self.command_text,
            "status": self.status,
            "allowed_tools": self.allowed_tools,
            "allowed_mcp_tools": self.allowed_mcp_tools,
            "exit_condition": self.exit_condition,
        }


@pytest.fixture
def mock_session_manager():
    mgr = MagicMock()
    mgr.resolve_session_reference = MagicMock(side_effect=lambda ref, project_id=None: ref)
    mgr.get = MagicMock(return_value=None)
    mgr.is_ancestor = MagicMock(return_value=False)
    return mgr


@pytest.fixture
def mock_message_manager():
    mgr = MagicMock()
    mgr.create_message = MagicMock()
    mgr.get_undelivered_messages = MagicMock(return_value=[])
    mgr.mark_delivered = MagicMock()
    mgr.list_messages = MagicMock(return_value=[])
    return mgr


@pytest.fixture
def mock_command_manager():
    mgr = MagicMock()
    mgr.create_command = MagicMock(return_value=MockCommand())
    mgr.get_command = MagicMock(return_value=None)
    mgr.list_commands = MagicMock(return_value=[])
    mgr.update_status = MagicMock(return_value=MockCommand(status="running"))
    return mgr


@pytest.fixture
def mock_session_var_manager():
    mgr = MagicMock()
    mgr.merge_variables = MagicMock(return_value=True)
    mgr.delete_variables = MagicMock()
    return mgr


@pytest.fixture
def mock_db():
    db = MagicMock()
    db.fetchone = MagicMock(return_value=None)
    db.execute = MagicMock()
    return db


@pytest.fixture
def messaging_registry(
    mock_session_manager,
    mock_message_manager,
    mock_command_manager,
    mock_session_var_manager,
    mock_db,
):
    from gobby.mcp_proxy.tools.agent_messaging import add_messaging_tools

    registry = InternalToolRegistry(
        name="gobby-agents",
        description="Agent messaging v2",
    )
    add_messaging_tools(
        registry=registry,
        message_manager=mock_message_manager,
        session_manager=mock_session_manager,
        command_manager=mock_command_manager,
        session_var_manager=mock_session_var_manager,
        db=mock_db,
    )
    return registry


# ═══════════════════════════════════════════════════════════════════════
# wait_for_command tests
# ═══════════════════════════════════════════════════════════════════════


class TestWaitForCommandImmediate:
    """Command already pending when wait_for_command is called."""

    @pytest.mark.asyncio
    async def test_immediate_return(
        self, messaging_registry, mock_command_manager
    ) -> None:
        """Returns immediately when a pending command exists."""
        cmd = MockCommand(id="cmd-1", to_session="s-child", command_text="Run tests")
        mock_command_manager.list_commands.return_value = [cmd]

        result = await messaging_registry.call(
            "wait_for_command",
            {"session_id": "s-child", "timeout": 60},
        )

        assert result["success"] is True
        assert result["timed_out"] is False
        assert result["command"]["id"] == "cmd-1"
        assert result["command"]["command_text"] == "Run tests"
        assert result["wait_time"] >= 0

    @pytest.mark.asyncio
    async def test_immediate_return_auto_activates(
        self, messaging_registry, mock_command_manager, mock_session_var_manager
    ) -> None:
        """Auto-activates the command by default."""
        cmd = MockCommand(id="cmd-1", to_session="s-child")
        mock_command_manager.list_commands.return_value = [cmd]

        result = await messaging_registry.call(
            "wait_for_command",
            {"session_id": "s-child"},
        )

        assert result["success"] is True
        mock_command_manager.update_status.assert_called_once_with("cmd-1", "running")
        mock_session_var_manager.merge_variables.assert_called_once()


class TestWaitForCommandPolling:
    """Command arrives after polling."""

    @pytest.mark.asyncio
    async def test_polls_until_found(
        self, messaging_registry, mock_command_manager
    ) -> None:
        """Finds command after a few poll cycles."""
        cmd = MockCommand(id="cmd-2", to_session="s-child", command_text="Deploy")
        # First call (initial check) returns empty, second call (after sleep) returns command
        mock_command_manager.list_commands.side_effect = [[], [cmd]]

        with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            result = await messaging_registry.call(
                "wait_for_command",
                {"session_id": "s-child", "timeout": 60, "poll_interval": 2},
            )

        assert result["success"] is True
        assert result["timed_out"] is False
        assert result["command"]["id"] == "cmd-2"
        mock_sleep.assert_called_once_with(2)

    @pytest.mark.asyncio
    async def test_polls_multiple_cycles(
        self, messaging_registry, mock_command_manager
    ) -> None:
        """Polls multiple times before command arrives."""
        cmd = MockCommand(id="cmd-3", to_session="s-child")
        # Empty for initial + 2 poll cycles, then found
        mock_command_manager.list_commands.side_effect = [[], [], [], [cmd]]

        with patch("asyncio.sleep", new_callable=AsyncMock):
            result = await messaging_registry.call(
                "wait_for_command",
                {"session_id": "s-child", "timeout": 300, "poll_interval": 1},
            )

        assert result["success"] is True
        assert result["timed_out"] is False
        assert result["command"]["id"] == "cmd-3"


class TestWaitForCommandTimeout:
    """No command arrives within timeout."""

    @pytest.mark.asyncio
    async def test_timeout_returns_none(
        self, messaging_registry, mock_command_manager
    ) -> None:
        """Returns timed_out=True and command=None when timeout expires."""
        mock_command_manager.list_commands.return_value = []

        with (
            patch("asyncio.sleep", new_callable=AsyncMock),
            patch("time.monotonic") as mock_time,
        ):
            # Start at 0, then jump past timeout on first poll check
            call_count = 0

            def mock_monotonic():
                nonlocal call_count
                call_count += 1
                return 0.0 if call_count <= 2 else 601.0

            mock_time.side_effect = mock_monotonic

            result = await messaging_registry.call(
                "wait_for_command",
                {"session_id": "s-child", "timeout": 600, "poll_interval": 5},
            )

        assert result["success"] is True
        assert result["timed_out"] is True
        assert result["command"] is None
        assert result["wait_time"] >= 600


class TestWaitForCommandAutoActivate:
    """Auto-activate behavior."""

    @pytest.mark.asyncio
    async def test_auto_activate_sets_variables(
        self,
        messaging_registry,
        mock_command_manager,
        mock_session_var_manager,
    ) -> None:
        """Auto-activate marks running and sets session variables."""
        cmd = MockCommand(
            id="cmd-1",
            to_session="s-child",
            command_text="Run tests",
            allowed_tools='["Read", "Grep"]',
            exit_condition="done",
        )
        mock_command_manager.list_commands.return_value = [cmd]

        result = await messaging_registry.call(
            "wait_for_command",
            {"session_id": "s-child", "auto_activate": True},
        )

        assert result["success"] is True
        mock_command_manager.update_status.assert_called_once_with("cmd-1", "running")
        merge_call = mock_session_var_manager.merge_variables.call_args
        variables = merge_call[0][1]
        assert variables["command_id"] == "cmd-1"
        assert variables["command_text"] == "Run tests"
        assert variables["allowed_tools"] == ["Read", "Grep"]
        assert variables["exit_condition"] == "done"

    @pytest.mark.asyncio
    async def test_no_auto_activate(
        self,
        messaging_registry,
        mock_command_manager,
        mock_session_var_manager,
    ) -> None:
        """When auto_activate=False, command stays pending."""
        cmd = MockCommand(id="cmd-1", to_session="s-child")
        mock_command_manager.list_commands.return_value = [cmd]

        result = await messaging_registry.call(
            "wait_for_command",
            {"session_id": "s-child", "auto_activate": False},
        )

        assert result["success"] is True
        assert result["command"]["id"] == "cmd-1"
        mock_command_manager.update_status.assert_not_called()
        mock_session_var_manager.merge_variables.assert_not_called()


class TestWaitForCommandPollInterval:
    """Poll interval floor enforcement."""

    @pytest.mark.asyncio
    async def test_negative_poll_interval_defaults(
        self, messaging_registry, mock_command_manager
    ) -> None:
        """Negative poll_interval defaults to 5s."""
        cmd = MockCommand(id="cmd-1", to_session="s-child")
        mock_command_manager.list_commands.side_effect = [[], [cmd]]

        with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            await messaging_registry.call(
                "wait_for_command",
                {"session_id": "s-child", "timeout": 60, "poll_interval": -1},
            )

        mock_sleep.assert_called_once_with(5)

    @pytest.mark.asyncio
    async def test_zero_poll_interval_defaults(
        self, messaging_registry, mock_command_manager
    ) -> None:
        """Zero poll_interval defaults to 5s."""
        cmd = MockCommand(id="cmd-1", to_session="s-child")
        mock_command_manager.list_commands.side_effect = [[], [cmd]]

        with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            await messaging_registry.call(
                "wait_for_command",
                {"session_id": "s-child", "timeout": 60, "poll_interval": 0},
            )

        mock_sleep.assert_called_once_with(5)


class TestWaitForCommandRegistration:
    """Tool is registered correctly."""

    def test_wait_for_command_registered(self, messaging_registry) -> None:
        tools = messaging_registry.list_tools()
        tool_names = {t["name"] for t in tools}
        assert "wait_for_command" in tool_names
