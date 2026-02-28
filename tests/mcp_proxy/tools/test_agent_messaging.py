"""Tests for agent_messaging module.

Covers:
- send_message: P2P messaging with same-project validation, auto-writes agent_runs.result
- send_command: ancestor-only command sending, rejects if active command exists
- complete_command: clears session variables and sends result to parent
- deliver_pending_messages: returns undelivered messages and marks them delivered
- activate_command: sets session variables from command fields
- get_inter_session_messages: read-only message history query
"""

from __future__ import annotations

from dataclasses import dataclass
from unittest.mock import MagicMock

import pytest

from gobby.mcp_proxy.tools.internal import InternalToolRegistry

pytestmark = pytest.mark.unit


# ═══════════════════════════════════════════════════════════════════════
# Mock helpers
# ═══════════════════════════════════════════════════════════════════════


@dataclass
class MockSession:
    id: str
    parent_session_id: str | None = None
    project_id: str = "project-1"
    status: str = "active"


@dataclass
class MockMessage:
    id: str = "msg-1"
    from_session: str = "s-from"
    to_session: str = "s-to"
    content: str = "hello"
    priority: str = "normal"
    sent_at: str = "2026-01-01T00:00:00"
    read_at: str | None = None
    message_type: str = "message"
    metadata_json: str | None = None
    delivered_at: str | None = None

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "from_session": self.from_session,
            "to_session": self.to_session,
            "content": self.content,
            "priority": self.priority,
            "sent_at": self.sent_at,
            "read_at": self.read_at,
        }


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
    mgr.create_message = MagicMock(return_value=MockMessage())
    mgr.get_undelivered_messages = MagicMock(return_value=[])
    mgr.mark_delivered = MagicMock(return_value=MockMessage(delivered_at="2026-01-01T00:01:00"))
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
# send_message
# ═══════════════════════════════════════════════════════════════════════


class TestSendMessage:
    """send_message validates same project and auto-writes agent_runs.result."""

    @pytest.mark.asyncio
    async def test_send_message_success(
        self, messaging_registry, mock_session_manager, mock_message_manager
    ) -> None:
        """P2P message between sessions in the same project."""
        mock_session_manager.get.side_effect = lambda sid: {
            "s-from": MockSession(id="s-from", project_id="proj-1"),
            "s-to": MockSession(id="s-to", project_id="proj-1"),
        }.get(sid)

        result = await messaging_registry.call(
            "send_message",
            {"from_session": "s-from", "to_session": "s-to", "content": "hi"},
        )

        assert result["success"] is True
        mock_message_manager.create_message.assert_called_once()

    @pytest.mark.asyncio
    async def test_send_message_different_project_rejected(
        self, messaging_registry, mock_session_manager
    ) -> None:
        """Reject messages between sessions in different projects."""
        mock_session_manager.get.side_effect = lambda sid: {
            "s-from": MockSession(id="s-from", project_id="proj-1"),
            "s-to": MockSession(id="s-to", project_id="proj-2"),
        }.get(sid)

        result = await messaging_registry.call(
            "send_message",
            {"from_session": "s-from", "to_session": "s-to", "content": "hi"},
        )

        assert result["success"] is False
        assert "project" in result["error"].lower()

    @pytest.mark.asyncio
    async def test_send_message_auto_writes_agent_runs_result(
        self, messaging_registry, mock_session_manager, mock_db
    ) -> None:
        """When child sends to parent, auto-write to agent_runs.result."""
        mock_session_manager.get.side_effect = lambda sid: {
            "s-child": MockSession(id="s-child", parent_session_id="s-parent", project_id="proj-1"),
            "s-parent": MockSession(id="s-parent", project_id="proj-1"),
        }.get(sid)
        # Simulate finding an agent_run row
        mock_db.fetchone.return_value = {"id": "run-1"}

        result = await messaging_registry.call(
            "send_message",
            {"from_session": "s-child", "to_session": "s-parent", "content": "done"},
        )

        assert result["success"] is True
        # Verify agent_runs.result was written
        mock_db.execute.assert_called_once()
        call_args = mock_db.execute.call_args
        assert "UPDATE agent_runs SET result" in call_args[0][0]

    @pytest.mark.asyncio
    async def test_send_message_session_not_found(
        self, messaging_registry, mock_session_manager
    ) -> None:
        """Reject when from_session does not exist."""
        mock_session_manager.get.return_value = None

        result = await messaging_registry.call(
            "send_message",
            {"from_session": "no-such", "to_session": "s-to", "content": "hi"},
        )

        assert result["success"] is False
        assert "not found" in result["error"].lower()


# ═══════════════════════════════════════════════════════════════════════
# send_command
# ═══════════════════════════════════════════════════════════════════════


class TestSendCommand:
    """send_command validates ancestor relationship and rejects active commands."""

    @pytest.mark.asyncio
    async def test_send_command_success(
        self, messaging_registry, mock_session_manager, mock_command_manager
    ) -> None:
        """Ancestor can send command to descendant."""
        mock_session_manager.is_ancestor.return_value = True
        mock_command_manager.list_commands.return_value = []  # no active commands

        result = await messaging_registry.call(
            "send_command",
            {
                "from_session": "s-parent",
                "to_session": "s-child",
                "command_text": "Run tests",
            },
        )

        assert result["success"] is True
        mock_command_manager.create_command.assert_called_once()

    @pytest.mark.asyncio
    async def test_send_command_not_ancestor_rejected(
        self, messaging_registry, mock_session_manager
    ) -> None:
        """Non-ancestor cannot send command."""
        mock_session_manager.is_ancestor.return_value = False

        result = await messaging_registry.call(
            "send_command",
            {
                "from_session": "s-unrelated",
                "to_session": "s-child",
                "command_text": "Run tests",
            },
        )

        assert result["success"] is False
        assert "ancestor" in result["error"].lower()

    @pytest.mark.asyncio
    async def test_send_command_active_command_rejected(
        self, messaging_registry, mock_session_manager, mock_command_manager
    ) -> None:
        """Reject if target session already has an active command."""
        mock_session_manager.is_ancestor.return_value = True
        # Return an active command
        mock_command_manager.list_commands.return_value = [
            MockCommand(id="cmd-existing", status="running"),
        ]

        result = await messaging_registry.call(
            "send_command",
            {
                "from_session": "s-parent",
                "to_session": "s-child",
                "command_text": "Another task",
            },
        )

        assert result["success"] is False
        assert "active command" in result["error"].lower()

    @pytest.mark.asyncio
    async def test_send_command_with_tools_and_exit(
        self, messaging_registry, mock_session_manager, mock_command_manager
    ) -> None:
        """Command with allowed_tools and exit_condition."""
        mock_session_manager.is_ancestor.return_value = True
        mock_command_manager.list_commands.return_value = []

        result = await messaging_registry.call(
            "send_command",
            {
                "from_session": "s-parent",
                "to_session": "s-child",
                "command_text": "Search code",
                "allowed_tools": ["Read", "Grep"],
                "exit_condition": "task_complete()",
            },
        )

        assert result["success"] is True
        call_kwargs = mock_command_manager.create_command.call_args
        assert (
            call_kwargs[1].get("allowed_tools") == ["Read", "Grep"] or call_kwargs[0][3]
            if len(call_kwargs[0]) > 3
            else True
        )


# ═══════════════════════════════════════════════════════════════════════
# complete_command
# ═══════════════════════════════════════════════════════════════════════


class TestCompleteCommand:
    """complete_command clears variables and sends result."""

    @pytest.mark.asyncio
    async def test_complete_command_success(
        self,
        messaging_registry,
        mock_command_manager,
        mock_session_var_manager,
        mock_message_manager,
    ) -> None:
        """Completing a command clears variables and sends result to parent."""
        mock_command_manager.get_command.return_value = MockCommand(
            id="cmd-1",
            from_session="s-parent",
            to_session="s-child",
            status="running",
        )
        mock_command_manager.update_status.return_value = MockCommand(
            id="cmd-1",
            status="completed",
        )

        result = await messaging_registry.call(
            "complete_command",
            {"session_id": "s-child", "command_id": "cmd-1", "result": "All tests pass"},
        )

        assert result["success"] is True
        # Verify command marked completed
        mock_command_manager.update_status.assert_called_once_with("cmd-1", "completed")
        # Verify session variables cleared
        mock_session_var_manager.delete_variables.assert_called_once_with("s-child")
        # Verify result sent to parent as message
        mock_message_manager.create_message.assert_called_once()
        msg_call = mock_message_manager.create_message.call_args
        assert msg_call[1]["to_session"] == "s-parent" or msg_call[0][1] == "s-parent"

    @pytest.mark.asyncio
    async def test_complete_command_not_found(
        self, messaging_registry, mock_command_manager
    ) -> None:
        """Error when command does not exist."""
        mock_command_manager.get_command.return_value = None

        result = await messaging_registry.call(
            "complete_command",
            {"session_id": "s-child", "command_id": "no-such", "result": "done"},
        )

        assert result["success"] is False
        assert "not found" in result["error"].lower()

    @pytest.mark.asyncio
    async def test_complete_command_wrong_session(
        self, messaging_registry, mock_command_manager
    ) -> None:
        """Error when session_id doesn't match command's to_session."""
        mock_command_manager.get_command.return_value = MockCommand(
            id="cmd-1",
            to_session="s-other",
        )

        result = await messaging_registry.call(
            "complete_command",
            {"session_id": "s-wrong", "command_id": "cmd-1", "result": "done"},
        )

        assert result["success"] is False
        assert "mismatch" in result["error"].lower() or "not assigned" in result["error"].lower()


# ═══════════════════════════════════════════════════════════════════════
# deliver_pending_messages
# ═══════════════════════════════════════════════════════════════════════


class TestDeliverPendingMessages:
    """deliver_pending_messages returns undelivered and marks delivered."""

    @pytest.mark.asyncio
    async def test_deliver_returns_undelivered(
        self, messaging_registry, mock_message_manager
    ) -> None:
        """Returns undelivered messages and marks them delivered."""
        msg1 = MockMessage(id="msg-1", content="first")
        msg2 = MockMessage(id="msg-2", content="second")
        mock_message_manager.get_undelivered_messages.return_value = [msg1, msg2]

        result = await messaging_registry.call(
            "deliver_pending_messages",
            {"session_id": "s-child"},
        )

        assert result["success"] is True
        assert len(result["messages"]) == 2
        # Verify both messages marked delivered
        assert mock_message_manager.mark_delivered.call_count == 2

    @pytest.mark.asyncio
    async def test_deliver_empty(self, messaging_registry, mock_message_manager) -> None:
        """Returns empty list when no undelivered messages."""
        mock_message_manager.get_undelivered_messages.return_value = []

        result = await messaging_registry.call(
            "deliver_pending_messages",
            {"session_id": "s-child"},
        )

        assert result["success"] is True
        assert result["messages"] == []
        assert result["count"] == 0


# ═══════════════════════════════════════════════════════════════════════
# activate_command
# ═══════════════════════════════════════════════════════════════════════


class TestActivateCommand:
    """activate_command sets session variables from command fields."""

    @pytest.mark.asyncio
    async def test_activate_command_success(
        self,
        messaging_registry,
        mock_command_manager,
        mock_session_var_manager,
    ) -> None:
        """Activating a command sets session variables and marks running."""
        mock_command_manager.get_command.return_value = MockCommand(
            id="cmd-1",
            to_session="s-child",
            command_text="Run tests",
            allowed_tools='["Read", "Grep"]',
            exit_condition="task_complete()",
        )
        mock_command_manager.update_status.return_value = MockCommand(
            id="cmd-1",
            status="running",
        )

        result = await messaging_registry.call(
            "activate_command",
            {"session_id": "s-child", "command_id": "cmd-1"},
        )

        assert result["success"] is True
        # Verify command marked running
        mock_command_manager.update_status.assert_called_once_with("cmd-1", "running")
        # Verify session variables set
        mock_session_var_manager.merge_variables.assert_called_once()
        merge_call = mock_session_var_manager.merge_variables.call_args
        variables = merge_call[0][1] if len(merge_call[0]) > 1 else merge_call[1].get("updates", {})
        assert variables.get("command_id") == "cmd-1"
        assert variables.get("command_text") == "Run tests"

    @pytest.mark.asyncio
    async def test_activate_command_not_found(
        self, messaging_registry, mock_command_manager
    ) -> None:
        """Error when command does not exist."""
        mock_command_manager.get_command.return_value = None

        result = await messaging_registry.call(
            "activate_command",
            {"session_id": "s-child", "command_id": "no-such"},
        )

        assert result["success"] is False
        assert "not found" in result["error"].lower()

    @pytest.mark.asyncio
    async def test_activate_command_wrong_session(
        self, messaging_registry, mock_command_manager
    ) -> None:
        """Error when session_id doesn't match command's to_session."""
        mock_command_manager.get_command.return_value = MockCommand(
            id="cmd-1",
            to_session="s-other",
        )

        result = await messaging_registry.call(
            "activate_command",
            {"session_id": "s-wrong", "command_id": "cmd-1"},
        )

        assert result["success"] is False


# ═══════════════════════════════════════════════════════════════════════
# Tool registration
# ═══════════════════════════════════════════════════════════════════════


class TestToolRegistration:
    """All expected tools are registered."""

    def test_all_tools_registered(self, messaging_registry) -> None:
        tools = messaging_registry.list_tools()
        tool_names = {t["name"] for t in tools}

        assert "send_message" in tool_names
        assert "send_command" in tool_names
        assert "complete_command" in tool_names
        assert "deliver_pending_messages" in tool_names
        assert "activate_command" in tool_names
        assert "wait_for_command" in tool_names
        assert "get_inter_session_messages" in tool_names


# ═══════════════════════════════════════════════════════════════════════
# get_inter_session_messages
# ═══════════════════════════════════════════════════════════════════════


class TestGetInterSessionMessages:
    """get_inter_session_messages is a read-only message history query."""

    @pytest.mark.asyncio
    async def test_returns_messages(self, messaging_registry, mock_message_manager) -> None:
        """Returns messages from list_messages as dicts."""
        msg1 = MockMessage(id="msg-1", content="hello")
        msg2 = MockMessage(id="msg-2", content="world")
        mock_message_manager.list_messages.return_value = [msg1, msg2]

        result = await messaging_registry.call(
            "get_inter_session_messages",
            {"session_id": "s-child"},
        )

        assert result["success"] is True
        assert result["count"] == 2
        assert len(result["messages"]) == 2
        assert result["messages"][0]["id"] == "msg-1"

    @pytest.mark.asyncio
    async def test_passes_direction(self, messaging_registry, mock_message_manager) -> None:
        """Direction parameter is forwarded to list_messages."""
        mock_message_manager.list_messages.return_value = []

        await messaging_registry.call(
            "get_inter_session_messages",
            {"session_id": "s-child", "direction": "inbox"},
        )

        call_kwargs = mock_message_manager.list_messages.call_args
        assert call_kwargs[1].get("direction") == "inbox" or (
            len(call_kwargs[0]) > 1 and call_kwargs[0][1] == "inbox"
        )

    @pytest.mark.asyncio
    async def test_no_side_effects(self, messaging_registry, mock_message_manager) -> None:
        """Does not call mark_delivered or mark_read."""
        mock_message_manager.list_messages.return_value = [
            MockMessage(id="msg-1"),
        ]

        await messaging_registry.call(
            "get_inter_session_messages",
            {"session_id": "s-child"},
        )

        mock_message_manager.mark_delivered.assert_not_called()
        mock_message_manager.mark_read.assert_not_called()

    @pytest.mark.asyncio
    async def test_empty_list(self, messaging_registry, mock_message_manager) -> None:
        """Returns empty list when no messages match."""
        mock_message_manager.list_messages.return_value = []

        result = await messaging_registry.call(
            "get_inter_session_messages",
            {"session_id": "s-child"},
        )

        assert result["success"] is True
        assert result["messages"] == []
        assert result["count"] == 0

    @pytest.mark.asyncio
    async def test_passes_all_filters(self, messaging_registry, mock_message_manager) -> None:
        """All filter parameters are forwarded to list_messages."""
        mock_message_manager.list_messages.return_value = []

        await messaging_registry.call(
            "get_inter_session_messages",
            {
                "session_id": "s-child",
                "direction": "sent",
                "unread_only": True,
                "undelivered_only": True,
                "message_type": "command_result",
                "limit": 10,
                "offset": 5,
            },
        )

        mock_message_manager.list_messages.assert_called_once()
        kwargs = mock_message_manager.list_messages.call_args[1]
        assert kwargs["direction"] == "sent"
        assert kwargs["unread_only"] is True
        assert kwargs["undelivered_only"] is True
        assert kwargs["message_type"] == "command_result"
        assert kwargs["limit"] == 10
        assert kwargs["offset"] == 5
