"""Tests for the shared web-chat session registry."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import MagicMock

import pytest

from gobby.hooks.events import HookEvent, HookEventType, HookResponse
from gobby.llm.claude_models import DoneEvent
from gobby.servers.websocket.chat._lifecycle import ChatLifecycleMixin
from gobby.servers.websocket.chat.session_registry import WebChatSessionRegistry
from gobby.storage.session_tasks import SessionTaskManager
from gobby.storage.tasks import LocalTaskManager
from gobby.workflows.engine.core import RuleEngine
from gobby.workflows.hooks import WorkflowHookHandler
from gobby.workflows.state_manager import SessionVariableManager
from gobby.workflows.sync_rules import get_bundled_rules_path, sync_bundled_rules
from tests._timing import drain_asyncio_tasks

pytestmark = pytest.mark.unit


async def _done_stream():
    yield DoneEvent(tool_calls_count=0)


class TestWebChatSessionRegistry:
    def test_lookup_by_conversation_id_and_db_session_id(self) -> None:
        registry = WebChatSessionRegistry()
        session = MagicMock()
        session.db_session_id = "db-id"
        session.conversation_id = "conv-1"

        registry.register("conv-1", session)

        assert registry.find_session("conv-1") == ("conv-1", session)
        assert registry.find_session("db-id") == ("conv-1", session)
        assert registry.find_session("missing") == (None, None)

    @pytest.mark.asyncio
    async def test_compact_session_drains_until_done_event(self) -> None:
        registry = WebChatSessionRegistry()
        session = MagicMock()
        session.db_session_id = "db-id"
        session.send_message.side_effect = lambda command: _done_stream()
        registry.register("conv-1", session)

        result = await registry.compact_session("db-id")

        assert result == {
            "compacted": True,
            "command": "/compact",
            "via": "web_chat",
            "queued": False,
        }
        assert [call.args[0] for call in session.send_message.call_args_list] == [
            "/compact",
            "Continue where you last left off.",
        ]

    @pytest.mark.asyncio
    async def test_active_session_queues_compaction_until_turn_completes(self) -> None:
        registry = WebChatSessionRegistry()
        session = MagicMock()
        session.db_session_id = "db-id"
        session.send_message.side_effect = lambda command: _done_stream()
        registry.register("conv-1", session)

        release = asyncio.Event()

        async def active_turn() -> None:
            await release.wait()

        active_task = asyncio.create_task(active_turn())
        registry.track_active_task("conv-1", active_task)

        result = await registry.compact_session("db-id")

        assert result["compacted"] is True
        assert result["queued"] is True
        session.send_message.assert_not_called()

        release.set()
        await active_task
        await drain_asyncio_tasks()
        queued_task = registry._queued_compaction_tasks.get("conv-1")
        assert queued_task is not None
        await queued_task
        assert [call.args[0] for call in session.send_message.call_args_list] == [
            "/compact",
            "Continue where you last left off.",
        ]

    @pytest.mark.asyncio
    async def test_drain_failure_returns_compacted_false(self) -> None:
        registry = WebChatSessionRegistry()
        session = MagicMock()
        session.db_session_id = "db-id"

        async def broken_stream():
            raise RuntimeError("boom")
            yield DoneEvent(tool_calls_count=0)

        session.send_message.side_effect = lambda command: broken_stream()
        registry.register("conv-1", session)

        result = await registry.compact_session("db-id")

        assert result["compacted"] is False
        assert "boom" in result["reason"]


class _LifecycleHost(ChatLifecycleMixin):
    def __init__(self) -> None:
        self.clients: dict[Any, dict[str, Any]] = {}
        self._chat_sessions: dict[str, Any] = {}
        self._active_chat_tasks: dict[str, asyncio.Task[None]] = {}
        self._pending_modes: dict[str, str] = {}
        self._pending_worktree_paths: dict[str, str] = {}
        self._pending_agents: dict[str, str] = {}
        self._pending_projects: dict[str, str] = {}
        self.workflow_handler: Any = None
        self.event_handlers: Any = None
        self.webhook_dispatcher: Any = None
        self.hook_broadcaster: Any = None
        self.inter_session_msg_manager: Any = None
        self.mcp_manager: Any = None
        self.internal_manager: Any = None
        self.captured_events: list[HookEvent] = []
        self.captured_mcp_calls: list[dict[str, Any]] = []

    def _inject_pending_messages(
        self,
        db_session_id: str,
        event_type: HookEventType,
    ) -> str | None:
        return None

    async def _dispatch_mcp_calls(
        self,
        mcp_calls: list[dict[str, Any]],
        event: HookEvent,
    ) -> None:
        self.captured_events.append(event)
        self.captured_mcp_calls.extend(mcp_calls)


def _web_chat_session(
    *,
    db_session_id: str = "db-id",
    project_id: str | None = "project-id",
) -> MagicMock:
    session = MagicMock()
    session.db_session_id = db_session_id
    session.seq_num = 42
    session.project_path = "/tmp/test-project"
    session.project_id = project_id
    session.provider = "claude"
    return session


class TestWebChatLifecycle:
    @pytest.mark.asyncio
    async def test_fire_lifecycle_adds_web_chat_session_type_metadata(self) -> None:
        host = _LifecycleHost()
        host._chat_sessions["conv-1"] = _web_chat_session()

        captured: list[HookEvent] = []

        def evaluate(event: HookEvent) -> HookResponse:
            captured.append(event)
            return HookResponse(decision="allow")

        workflow_handler = MagicMock()
        workflow_handler.evaluate.side_effect = evaluate
        host.workflow_handler = workflow_handler

        result = await host._fire_lifecycle(
            "conv-1",
            HookEventType.AFTER_TOOL,
            {"tool_name": "mcp__gobby__call_tool"},
        )

        assert result is not None
        assert captured[0].metadata["session_type"] == "web_chat"

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_after_tool_close_task_with_remaining_epic_queues_compact_self(
        self,
        temp_db,
        sample_project,
    ) -> None:
        sync_bundled_rules(temp_db, get_bundled_rules_path())

        task_manager = LocalTaskManager(temp_db)
        closed_task = task_manager.create_task(
            project_id=sample_project["id"],
            title="Closed task",
            task_type="task",
        )
        remaining_epic = task_manager.create_task(
            project_id=sample_project["id"],
            title="Remaining epic",
            task_type="epic",
        )

        SessionVariableManager(temp_db).merge_variables(
            "db-id",
            {
                "claimed_tasks": {
                    closed_task.id: f"#{closed_task.seq_num}",
                    remaining_epic.id: f"#{remaining_epic.seq_num}",
                },
                "task_claimed": True,
            },
        )

        host = _LifecycleHost()
        host._chat_sessions["conv-1"] = _web_chat_session(project_id=sample_project["id"])
        rule_engine = RuleEngine(temp_db, task_manager=task_manager)
        host.workflow_handler = WorkflowHookHandler(
            rule_engine=rule_engine,
            task_manager=task_manager,
            session_task_manager=SessionTaskManager(temp_db),
        )

        result = await host._fire_lifecycle(
            "conv-1",
            HookEventType.AFTER_TOOL,
            {
                "tool_name": "mcp__gobby__call_tool",
                "tool_input": {
                    "server_name": "gobby-tasks",
                    "tool_name": "close_task",
                    "arguments": {"task_id": closed_task.id},
                },
                "tool_output": {"success": True, "result": {"id": closed_task.id}},
            },
        )

        assert result is not None
        compact_calls = [
            call
            for call in host.captured_mcp_calls
            if call.get("server") == "gobby-sessions" and call.get("tool") == "compact_self"
        ]
        assert compact_calls == [
            {
                "server": "gobby-sessions",
                "tool": "compact_self",
                "arguments": {"session_id": "db-id"},
                "background": True,
                "inject_result": False,
                "block_on_failure": False,
                "block_on_success": False,
            }
        ]
