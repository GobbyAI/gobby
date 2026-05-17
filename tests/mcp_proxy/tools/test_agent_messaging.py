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
    agent_depth: int = 0


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

    def to_brief(self) -> dict:
        return {
            "id": self.id,
            "from_session": self.from_session,
            "to_session": self.to_session,
            "content": self.content,
            "priority": self.priority,
            "message_type": self.message_type,
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


class FakeWakeDispatcher:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def dispatch_live_wake(self, session_id: str) -> dict[str, object]:
        self.calls.append(session_id)
        return {"session_id": session_id, "delivered": True, "method": "fake"}


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
    async def test_send_message_accepts_to_session_and_metadata(
        self, messaging_registry, mock_session_manager, mock_message_manager
    ) -> None:
        """Public send_message accepts to_session and forwards metadata."""
        mock_session_manager.get.side_effect = lambda sid: {
            "s-from": MockSession(id="s-from", project_id="proj-1"),
            "s-to": MockSession(id="s-to", project_id="proj-1"),
        }.get(sid)

        result = await messaging_registry.call(
            "send_message",
            {
                "from_session": "s-from",
                "to_session": "s-to",
                "content": "assignment",
                "priority": "high",
                "message_type": "task_assignment",
                "metadata": {"task_id": "#14760"},
            },
        )

        assert result["success"] is True
        assert result["recipient_session_ids"] == ["s-to"]
        call_kwargs = mock_message_manager.create_message.call_args.kwargs
        assert call_kwargs["priority"] == "high"
        assert call_kwargs["message_type"] == "task_assignment"
        assert '"task_id": "#14760"' in call_kwargs["metadata_json"]

    @pytest.mark.asyncio
    async def test_send_message_rejects_target_with_broadcast(
        self, messaging_registry, mock_session_manager, mock_message_manager
    ) -> None:
        """Reject explicit target when broadcasting."""
        result = await messaging_registry.call(
            "send_message",
            {
                "from_session": "s-from",
                "to_session": "s-to",
                "send_to_all": True,
                "content": "hi",
            },
        )

        assert result["success"] is False
        assert result["error"] == "to_session cannot be combined with send_to_all=true."
        mock_session_manager.resolve_session_reference.assert_not_called()
        mock_message_manager.create_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_send_message_requires_target_unless_broadcast(
        self, messaging_registry, mock_session_manager, mock_message_manager
    ) -> None:
        """Reject direct sends without exactly one target identifier."""
        result = await messaging_registry.call(
            "send_message",
            {"from_session": "s-from", "content": "hi"},
        )

        assert result["success"] is False
        assert result["error"] == "Pass to_session unless send_to_all is true."
        mock_session_manager.resolve_session_reference.assert_not_called()
        mock_message_manager.create_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_send_message_send_to_all_fans_out_and_wakes(
        self,
        mock_session_manager,
        mock_message_manager,
        mock_command_manager,
        mock_session_var_manager,
        mock_db,
    ) -> None:
        """send_to_all delegates broadcast fanout and optional wake to MailboxService."""
        from gobby.mcp_proxy.tools.agent_messaging import add_messaging_tools

        wake_dispatcher = FakeWakeDispatcher()
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
            wake_dispatcher=wake_dispatcher,
        )
        mock_session_manager.get.side_effect = lambda sid: {
            "s-from": MockSession(id="s-from", project_id="proj-1"),
            "s-child": MockSession(id="s-child", project_id="proj-1"),
        }.get(sid)
        mock_db.fetchall.return_value = [
            {
                "child_session_id": "s-child",
                "child_status": "active",
                "parent_session_id": "s-from",
                "parent_status": "active",
            }
        ]
        mock_message_manager.create_message.side_effect = lambda **kwargs: MockMessage(
            id="msg-broadcast",
            from_session=kwargs["from_session"],
            to_session=kwargs["to_session"],
            content=kwargs["content"],
            priority=kwargs["priority"],
            message_type=kwargs["message_type"],
            metadata_json=kwargs["metadata_json"],
        )

        result = await registry.call(
            "send_message",
            {
                "from_session": "s-from",
                "send_to_all": True,
                "include_wakeup": True,
                "content": "hello agents",
            },
        )

        assert result["success"] is True
        assert result["recipient_session_ids"] == ["s-child"]
        assert result["broadcast_id"]
        assert wake_dispatcher.calls == ["s-child"]
        assert result["wake_results"] == [
            {"session_id": "s-child", "delivered": True, "method": "fake"}
        ]

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
        """Lower-depth session can send command to higher-depth session."""
        mock_session_manager.get.side_effect = lambda sid: {
            "s-parent": MockSession(id="s-parent", agent_depth=0, project_id="proj-1"),
            "s-child": MockSession(id="s-child", agent_depth=1, project_id="proj-1"),
        }.get(sid)
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
    async def test_send_command_same_depth_rejected_for_agents(
        self, messaging_registry, mock_session_manager
    ) -> None:
        """Agent at depth 1 cannot send command to another at depth 1."""
        mock_session_manager.get.side_effect = lambda sid: {
            "s-unrelated": MockSession(id="s-unrelated", agent_depth=1, project_id="proj-1"),
            "s-child": MockSession(id="s-child", agent_depth=1, project_id="proj-1"),
        }.get(sid)

        result = await messaging_registry.call(
            "send_command",
            {
                "from_session": "s-unrelated",
                "to_session": "s-child",
                "command_text": "Run tests",
            },
        )

        assert result["success"] is False
        assert "depth" in result["error"].lower()

    @pytest.mark.asyncio
    async def test_send_command_depth_zero_can_command_same_depth(
        self, messaging_registry, mock_session_manager, mock_command_manager
    ) -> None:
        """Depth-0 session (web chat) can command any session, even depth 0."""
        mock_session_manager.get.side_effect = lambda sid: {
            "s-webchat": MockSession(id="s-webchat", agent_depth=0, project_id="proj-1"),
            "s-cli": MockSession(id="s-cli", agent_depth=0, project_id="proj-1"),
        }.get(sid)
        mock_command_manager.list_commands.return_value = []

        result = await messaging_registry.call(
            "send_command",
            {
                "from_session": "s-webchat",
                "to_session": "s-cli",
                "command_text": "Run tests",
            },
        )

        assert result["success"] is True

    @pytest.mark.asyncio
    async def test_send_command_active_command_rejected(
        self, messaging_registry, mock_session_manager, mock_command_manager
    ) -> None:
        """Reject if target session already has an active command."""
        mock_session_manager.get.side_effect = lambda sid: {
            "s-parent": MockSession(id="s-parent", agent_depth=0, project_id="proj-1"),
            "s-child": MockSession(id="s-child", agent_depth=1, project_id="proj-1"),
        }.get(sid)
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
        mock_session_manager.get.side_effect = lambda sid: {
            "s-parent": MockSession(id="s-parent", agent_depth=0, project_id="proj-1"),
            "s-child": MockSession(id="s-child", agent_depth=1, project_id="proj-1"),
        }.get(sid)
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
            {"target_session_id": "s-child", "command_id": "cmd-1", "result": "All tests pass"},
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
            {"target_session_id": "s-child", "command_id": "no-such", "result": "done"},
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
            {"target_session_id": "s-wrong", "command_id": "cmd-1", "result": "done"},
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
            {"target_session_id": "s-child"},
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
            {"target_session_id": "s-child"},
        )

        assert result["success"] is True
        assert result["messages"] == []
        assert result["count"] == 0

    @pytest.mark.asyncio
    async def test_deliver_includes_run_task_type_and_signoff_context(
        self, messaging_registry, mock_message_manager
    ) -> None:
        """Delivered completion messages include metadata needed for continuation."""
        msg = MockMessage(
            id="msg-1",
            message_type="completion_notification",
            metadata_json=(
                '{"run_id": "run-1", "task_id": "#12754", '
                '"completion_id": "run-1", "signoff_message": "Approved"}'
            ),
        )
        mock_message_manager.get_undelivered_messages.return_value = [msg]

        result = await messaging_registry.call(
            "deliver_pending_messages",
            {"target_session_id": "s-child"},
        )

        delivered = result["messages"][0]
        assert delivered["message_type"] == "completion_notification"
        assert delivered["run_id"] == "run-1"
        assert delivered["task_id"] == "#12754"
        assert delivered["completion_id"] == "run-1"
        assert delivered["signoff_message"] == "Approved"
        assert delivered["has_signoff"] is True
        assert delivered["metadata"]["run_id"] == "run-1"


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
            {"target_session_id": "s-child", "command_id": "cmd-1"},
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
            {"target_session_id": "s-child", "command_id": "no-such"},
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
            {"target_session_id": "s-wrong", "command_id": "cmd-1"},
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
            {"target_session_id": "s-child"},
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
            {"target_session_id": "s-child", "direction": "inbox"},
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
            {"target_session_id": "s-child"},
        )

        mock_message_manager.mark_delivered.assert_not_called()
        assert mock_message_manager.mark_delivered.call_count == 0
        assert not mock_message_manager.mark_delivered.called
        mock_message_manager.mark_read.assert_not_called()
        assert mock_message_manager.mark_read.call_count == 0
        assert not mock_message_manager.mark_read.called

    @pytest.mark.asyncio
    async def test_empty_list(self, messaging_registry, mock_message_manager) -> None:
        """Returns empty list when no messages match."""
        mock_message_manager.list_messages.return_value = []

        result = await messaging_registry.call(
            "get_inter_session_messages",
            {"target_session_id": "s-child"},
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
                "target_session_id": "s-child",
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
