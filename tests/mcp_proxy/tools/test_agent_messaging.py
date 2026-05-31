"""Tests for agent_messaging module.

Covers:
    - send_message: target-based messaging, auto-writes agent_runs.result
- deliver_pending_messages: returns undelivered messages and marks them delivered
- get_inter_session_messages: read-only message history query
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path
from typing import Any
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
    terminal_context: dict[str, Any] | None = None


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
            "delivered_at": self.delivered_at,
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


class FakeWakeDispatcher:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def dispatch_live_wake(self, session_id: str) -> dict[str, Any]:
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
def mock_db():
    db = MagicMock()
    db.fetchone = MagicMock(return_value=None)
    db.execute = MagicMock()
    return db


@pytest.fixture
def messaging_registry(
    mock_session_manager,
    mock_message_manager,
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
        db=mock_db,
    )
    return registry


# ═══════════════════════════════════════════════════════════════════════
# send_message
# ═══════════════════════════════════════════════════════════════════════


class TestSendMessage:
    """send_message resolves explicit targets and auto-writes agent_runs.result."""

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
            {
                "from_session": "s-from",
                "target": "session",
                "target_id": "s-to",
                "content": "hi",
            },
        )

        assert result["success"] is True
        mock_message_manager.create_message.assert_called_once()

    @pytest.mark.asyncio
    async def test_send_message_accepts_session_target_and_metadata(
        self, messaging_registry, mock_session_manager, mock_message_manager
    ) -> None:
        """Public send_message accepts session target_id and forwards metadata."""
        mock_session_manager.get.side_effect = lambda sid: {
            "s-from": MockSession(id="s-from", project_id="proj-1"),
            "s-to": MockSession(id="s-to", project_id="proj-1"),
        }.get(sid)

        result = await messaging_registry.call(
            "send_message",
            {
                "from_session": "s-from",
                "target": "session",
                "target_id": "s-to",
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
    async def test_send_message_defaults_from_session_from_context(
        self, messaging_registry, mock_session_manager, mock_message_manager
    ) -> None:
        """Omitted from_session resolves from the caller's SessionContext."""
        from gobby.utils.session_context import session_context_for_test

        mock_session_manager.get.side_effect = lambda sid: {
            "s-from": MockSession(id="s-from", project_id="proj-1"),
            "s-to": MockSession(id="s-to", project_id="proj-1"),
        }.get(sid)
        mock_message_manager.create_message.side_effect = lambda **kwargs: MockMessage(
            id="msg-context",
            from_session=kwargs["from_session"],
            to_session=kwargs["to_session"],
            content=kwargs["content"],
            priority=kwargs["priority"],
            message_type=kwargs["message_type"],
            metadata_json=kwargs["metadata_json"],
        )

        with session_context_for_test("s-from"):
            result = await messaging_registry.call(
                "send_message",
                {"target": "session", "target_id": "s-to", "content": "context send"},
            )

        assert result["success"] is True
        assert result["recipient_session_ids"] == ["s-to"]
        assert result["message"]["from_session"] == "s-from"
        call_kwargs = mock_message_manager.create_message.call_args.kwargs
        assert call_kwargs["from_session"] == "s-from"
        assert call_kwargs["to_session"] == "s-to"
        assert call_kwargs["content"] == "context send"

    @pytest.mark.asyncio
    async def test_send_message_no_session_context_returns_error(
        self, messaging_registry, mock_session_manager, mock_message_manager
    ) -> None:
        """Omitted from_session outside SessionContext returns a tool error."""
        result = await messaging_registry.call(
            "send_message",
            {"target": "session", "target_id": "s-to", "content": "hi"},
        )

        assert result == {
            "success": False,
            "error": "from_session is required and no SessionContext session_id is available",
        }
        mock_session_manager.resolve_session_reference.assert_not_called()
        mock_message_manager.create_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_send_message_direct_function_accepts_keyword_priority(
        self, messaging_registry, mock_session_manager, mock_message_manager
    ) -> None:
        """Direct send_message function accepts optional fields only by keyword."""
        mock_session_manager.get.side_effect = lambda sid: {
            "s-from": MockSession(id="s-from", project_id="proj-1"),
            "s-to": MockSession(id="s-to", project_id="proj-1"),
        }.get(sid)
        send_message = messaging_registry.get_tool("send_message")
        assert send_message is not None

        result = await send_message(
            "session", "current", "s-to", from_session="s-from", priority="high"
        )

        assert result["success"] is True
        call_kwargs = mock_message_manager.create_message.call_args.kwargs
        assert call_kwargs["to_session"] == "s-to"
        assert call_kwargs["content"] == "current"
        assert call_kwargs["priority"] == "high"

    @pytest.mark.asyncio
    async def test_send_message_rejects_positional_priority(
        self, messaging_registry, mock_session_manager
    ) -> None:
        """Direct send_message no longer accepts legacy positional priority."""
        send_message = messaging_registry.get_tool("send_message")
        assert send_message is not None

        with pytest.raises(TypeError, match="positional"):
            await send_message("session", "legacy", "s-to", "s-from", "high")

        mock_session_manager.resolve_session_reference.assert_not_called()

    def test_send_message_schema_documents_target_parameters(self, messaging_registry) -> None:
        """Tool description names target args and keyword-only optional fields."""
        schema = messaging_registry.get_schema("send_message")

        assert schema is not None
        description = schema["description"]
        assert "keyword-only" in description
        assert (
            "from_session defaults to the calling session's id from SessionContext" in description
        )
        assert "target='session'" in description
        assert "target='all' forbids target_id" in description
        assert "target" in schema["inputSchema"]["properties"]
        assert "target_id" in schema["inputSchema"]["properties"]
        assert "from_session" in schema["inputSchema"]["properties"]
        assert "project_id" in schema["inputSchema"]["properties"]
        assert "from_session" not in schema["inputSchema"]["required"]
        assert "to_session" not in schema["inputSchema"]["properties"]
        assert "send_to_all" not in schema["inputSchema"]["properties"]

    def test_no_positional_from_session_in_production_callers(self) -> None:
        """Production callers do not pass from_session as send_message's first positional arg."""
        repo_root = Path(__file__).resolve().parents[3]
        offenders: list[str] = []

        for path in (repo_root / "src").rglob("*.py"):
            tree = ast.parse(path.read_text(), filename=str(path))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call) or len(node.args) < 3:
                    continue

                func = node.func
                if isinstance(func, ast.Name):
                    call_name = func.id
                elif isinstance(func, ast.Attribute):
                    call_name = func.attr
                else:
                    continue

                if call_name != "send_message":
                    continue

                first_arg = ast.unparse(node.args[0])
                if "from_session" in first_arg or "session_id" in first_arg:
                    rel_path = path.relative_to(repo_root)
                    offenders.append(f"{rel_path}:{node.lineno}: {first_arg}")

        assert offenders == []

    @pytest.mark.asyncio
    async def test_send_message_rejects_target_id_with_all(
        self, messaging_registry, mock_session_manager, mock_message_manager
    ) -> None:
        """Reject target_id when sending to all."""
        result = await messaging_registry.call(
            "send_message",
            {
                "from_session": "s-from",
                "target": "all",
                "target_id": "s-to",
                "content": "hi",
            },
        )

        assert result["success"] is False
        assert result["error"] == "target_id is not allowed when target='all'."
        mock_session_manager.resolve_session_reference.assert_not_called()
        mock_message_manager.create_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_send_message_requires_target_id_for_session_target(
        self, messaging_registry, mock_session_manager, mock_message_manager
    ) -> None:
        """Reject session target sends without a target identifier."""
        result = await messaging_registry.call(
            "send_message",
            {"from_session": "s-from", "target": "session", "content": "hi"},
        )

        assert result["success"] is False
        assert result["error"] == "target_id is required when target='session'."
        mock_session_manager.resolve_session_reference.assert_not_called()
        mock_message_manager.create_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_send_message_rejects_unknown_target(
        self, messaging_registry, mock_message_manager
    ) -> None:
        """Reject unknown target selectors."""
        result = await messaging_registry.call(
            "send_message",
            {"from_session": "s-from", "target": "workspace", "content": "hi"},
        )

        assert result["success"] is False
        assert "Unknown message target" in result["error"]
        mock_message_manager.create_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_send_message_rejects_unknown_project_target_id(
        self, messaging_registry, mock_session_manager, mock_message_manager
    ) -> None:
        """Reject unknown target identifiers."""
        mock_session_manager.get.side_effect = lambda sid: {
            "s-from": MockSession(id="s-from", project_id="proj-1"),
        }.get(sid)

        result = await messaging_registry.call(
            "send_message",
            {
                "from_session": "s-from",
                "target": "project",
                "target_id": "missing-project",
                "content": "hi",
            },
        )

        assert result["success"] is False
        assert "Project target not found" in result["error"]
        mock_message_manager.create_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_send_message_rejects_empty_content(
        self, messaging_registry, mock_session_manager, mock_message_manager
    ) -> None:
        """Reject blank content before resolving sessions."""
        result = await messaging_registry.call(
            "send_message",
            {
                "from_session": "s-from",
                "target": "all",
                "content": "  ",
            },
        )

        assert result["success"] is False
        assert result["error"] == "content is required."
        mock_session_manager.resolve_session_reference.assert_not_called()
        mock_message_manager.create_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_send_message_project_target_fans_out_and_wakes(
        self,
        mock_session_manager,
        mock_message_manager,
        mock_db,
    ) -> None:
        """Project target delegates fanout and optional wake to MailboxService."""
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
            db=mock_db,
            wake_dispatcher=wake_dispatcher,
        )
        mock_session_manager.get.side_effect = lambda sid: {
            "s-from": MockSession(id="s-from", project_id="proj-1"),
            "s-child": MockSession(id="s-child", project_id="proj-1"),
        }.get(sid)
        mock_db.fetchone.side_effect = lambda sql, params=(): (
            {"id": "proj-1"} if "FROM projects" in sql else None
        )
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
                "target": "project",
                "target_id": "proj-1",
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
    async def test_send_message_build_target_uses_context_project_for_coordinator(
        self,
        messaging_registry,
        mock_session_manager,
        mock_message_manager,
        mock_db,
    ) -> None:
        """Wrapper project context scopes build targets for cross-project coordinators."""
        from gobby.utils.project_context import reset_project_context, set_project_context

        mock_session_manager.get.side_effect = lambda sid: {
            "s-coord": MockSession(id="s-coord", project_id="proj-coord"),
            "s-child": MockSession(id="s-child", project_id="proj-target"),
        }.get(sid)

        def fetchone(sql: str, params: tuple[Any, ...] = ()) -> dict[str, Any] | None:
            if "FROM projects" in sql:
                return {"id": params[0]} if params and params[0] == "proj-target" else None
            if "FROM build_runs WHERE id = %s" in sql:
                return None
            if "SELECT id FROM tasks WHERE project_id = %s AND seq_num = %s" in sql:
                return {"id": "task-root"} if params == ("proj-target", 354) else None
            if "SELECT project_id FROM tasks WHERE id = %s" in sql:
                return {"project_id": "proj-target"}
            if "SELECT *" in sql and "FROM build_runs" in sql:
                return {
                    "id": "br-1",
                    "project_id": "proj-target",
                    "root_task_id": "task-root",
                    "input_ref": "#354",
                    "action": "build",
                    "status": "started",
                    "actor": "build",
                    "summary_json": {
                        "build_project_id": "proj-target",
                        "coordinator_project_id": "proj-coord",
                        "coordinator_session_id": "s-coord",
                    },
                    "error": None,
                    "started_at": "2026-01-01T00:00:00",
                    "completed_at": None,
                }
            return None

        def fetchall(sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
            if "FROM build_runs" in sql and "input_ref" in sql:
                return []
            if "WITH RECURSIVE ancestors" in sql:
                return [{"id": "task-root"}]
            if "FROM agent_runs" in sql:
                return [
                    {
                        "child_session_id": "s-child",
                        "child_status": "active",
                        "parent_session_id": "s-coord",
                        "parent_status": "active",
                    }
                ]
            return []

        mock_db.fetchone.side_effect = fetchone
        mock_db.fetchall.side_effect = fetchall
        mock_message_manager.create_message.side_effect = lambda **kwargs: MockMessage(
            id="msg-build",
            from_session=kwargs["from_session"],
            to_session=kwargs["to_session"],
            content=kwargs["content"],
            priority=kwargs["priority"],
            message_type=kwargs["message_type"],
            metadata_json=kwargs["metadata_json"],
        )

        token = set_project_context({"id": "proj-target", "name": "Target"})
        try:
            result = await messaging_registry.call(
                "send_message",
                {
                    "from_session": "s-coord",
                    "target": "build",
                    "target_id": "#354",
                    "content": "daemon restart warning",
                },
            )
        finally:
            reset_project_context(token)

        assert result["success"] is True
        assert result["recipient_session_ids"] == ["s-child"]
        assert result["selector_metadata"]["project_id"] == "proj-target"
        mock_message_manager.create_message.assert_called_once()

    @pytest.mark.asyncio
    async def test_send_message_agent_target_allows_cross_project_coordinator(
        self,
        messaging_registry,
        mock_session_manager,
        mock_message_manager,
        mock_db,
    ) -> None:
        """Recorded build coordinators may message their build agents directly."""
        mock_session_manager.get.side_effect = lambda sid: {
            "s-coord": MockSession(id="s-coord", project_id="proj-coord"),
            "s-child": MockSession(id="s-child", project_id="proj-target"),
        }.get(sid)

        def fetchone(sql: str, params: tuple[Any, ...] = ()) -> dict[str, Any] | None:
            if "FROM projects" in sql:
                return {"id": params[0]} if params and params[0] == "proj-target" else None
            if "FROM agent_runs ar" in sql:
                return {
                    "id": "run-1",
                    "status": "running",
                    "task_id": "task-leaf",
                    "child_session_id": "s-child",
                    "parent_session_id": "s-coord",
                    "child_status": "active",
                    "parent_status": "active",
                }
            if "SELECT project_id FROM tasks WHERE id = %s" in sql:
                return {"project_id": "proj-target"}
            if "SELECT *" in sql and "FROM build_runs" in sql:
                return {
                    "id": "br-1",
                    "project_id": "proj-target",
                    "root_task_id": "task-root",
                    "input_ref": "#354",
                    "action": "build",
                    "status": "started",
                    "actor": "build",
                    "summary_json": {
                        "build_project_id": "proj-target",
                        "coordinator_project_id": "proj-coord",
                        "coordinator_session_id": "s-coord",
                    },
                    "error": None,
                    "started_at": "2026-01-01T00:00:00",
                    "completed_at": None,
                }
            return None

        mock_db.fetchone.side_effect = fetchone
        mock_db.fetchall.side_effect = lambda sql, params=(): (
            [{"id": "task-leaf"}, {"id": "task-root"}] if "WITH RECURSIVE ancestors" in sql else []
        )
        mock_message_manager.create_message.side_effect = lambda **kwargs: MockMessage(
            id="msg-agent",
            from_session=kwargs["from_session"],
            to_session=kwargs["to_session"],
            content=kwargs["content"],
            priority=kwargs["priority"],
            message_type=kwargs["message_type"],
            metadata_json=kwargs["metadata_json"],
        )

        result = await messaging_registry.call(
            "send_message",
            {
                "from_session": "s-coord",
                "target": "agent",
                "target_id": "run-1",
                "project_id": "proj-target",
                "content": "daemon restart warning",
            },
        )

        assert result["success"] is True
        assert result["recipient_session_ids"] == ["s-child"]
        assert result["selector_metadata"]["task_id"] == "task-leaf"
        mock_message_manager.create_message.assert_called_once()

    @pytest.mark.asyncio
    async def test_send_message_persists_when_live_wake_has_no_tmux_pane(
        self,
        mock_session_manager,
        mock_message_manager,
        mock_db,
    ) -> None:
        """include_wakeup stores mailbox rows even when no live pane exists."""
        from gobby.events.wake import WakeDispatcher
        from gobby.mcp_proxy.tools.agent_messaging import add_messaging_tools

        registry = InternalToolRegistry(
            name="gobby-agents",
            description="Agent messaging v2",
        )
        wake_dispatcher = WakeDispatcher(
            session_manager=mock_session_manager,
            ism_manager=mock_message_manager,
            tmux_pane_sender=MagicMock(),
        )
        add_messaging_tools(
            registry=registry,
            message_manager=mock_message_manager,
            session_manager=mock_session_manager,
            db=mock_db,
            wake_dispatcher=wake_dispatcher,
        )

        created = MockMessage(id="msg-direct", delivered_at=None)
        mock_message_manager.create_message.return_value = created
        mock_session_manager.get.side_effect = lambda sid: {
            "s-from": MockSession(id="s-from", project_id="proj-1"),
            "s-to": MockSession(
                id="s-to",
                project_id="proj-1",
                terminal_context={"parent_pid": 12345},
            ),
        }.get(sid)

        result = await registry.call(
            "send_message",
            {
                "from_session": "s-from",
                "target": "session",
                "target_id": "s-to",
                "include_wakeup": True,
                "content": "hello",
            },
        )

        assert result["success"] is True
        assert result["message_ids"] == ["msg-direct"]
        assert result["message"]["delivered_at"] is None
        assert result["wake_results"][0]["error_code"] == "no_tmux_pane"
        mock_message_manager.mark_delivered.assert_not_called()

        mock_message_manager.get_undelivered_messages.return_value = [created]
        delivered = await registry.call(
            "deliver_pending_messages",
            {"target_session_id": "s-to"},
        )

        assert delivered["success"] is True
        assert delivered["count"] == 1
        mock_message_manager.mark_delivered.assert_called_once_with("msg-direct")

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
            {
                "from_session": "s-from",
                "target": "session",
                "target_id": "s-to",
                "content": "hi",
            },
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
            {
                "from_session": "s-child",
                "target": "session",
                "target_id": "s-parent",
                "content": "done",
            },
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
            {
                "from_session": "no-such",
                "target": "session",
                "target_id": "s-to",
                "content": "hi",
            },
        )

        assert result["success"] is False
        assert "not found" in result["error"].lower()


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
# Tool registration
# ═══════════════════════════════════════════════════════════════════════


class TestToolRegistration:
    """All expected tools are registered."""

    def test_all_tools_registered(self, messaging_registry) -> None:
        tools = messaging_registry.list_tools()
        tool_names = {t["name"] for t in tools}

        assert "send_message" in tool_names
        assert "deliver_pending_messages" in tool_names
        assert "get_inter_session_messages" in tool_names
        assert "send_command" not in tool_names
        assert "activate_command" not in tool_names
        assert "complete_command" not in tool_names
        assert "wait_for_command" not in tool_names


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
        """direction='inbox' is forwarded to list_messages."""
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
    async def test_received_direction_aliases_inbox(
        self,
        messaging_registry,
        mock_message_manager,
    ) -> None:
        """direction='received' is normalized before storage query."""
        mock_message_manager.list_messages.return_value = []

        await messaging_registry.call(
            "get_inter_session_messages",
            {"target_session_id": "s-child", "direction": "received"},
        )

        kwargs = mock_message_manager.list_messages.call_args.kwargs
        assert kwargs["direction"] == "inbox"

    @pytest.mark.asyncio
    async def test_invalid_direction_is_rejected(
        self,
        messaging_registry,
        mock_message_manager,
    ) -> None:
        """Invalid direction returns a clear error without querying storage."""
        result = await messaging_registry.call(
            "get_inter_session_messages",
            {"target_session_id": "s-child", "direction": "bogus"},
        )

        assert result["success"] is False
        assert result["error_code"] == "invalid_direction"
        assert "Invalid direction 'bogus'" in result["error"]
        mock_message_manager.list_messages.assert_not_called()

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
