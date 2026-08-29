"""
Tests for agents.py MCP tools module.

This file tests the agent-related MCP tools:
- spawn_agent: Spawn a subagent with isolation support
- get_agent_result: Get agent run result
- wait_for_agent: Wait for agent run completion
- wait_for_output: Wait for a terminal-output regex match
- list_agent_runs: List agent runs for a session
- stop_agent: Stop a running agent (DB only)
- end_agent_run: Complete the caller's own agent run
- kill_agent: Kill a running agent process
- can_spawn_agent: Check if spawning is allowed
- list_running_agents: List active agents from DB
- get_running_agent: Get running agent state from DB
- unregister_agent: Mark agent as failed in DB
- running_agent_stats: Get agent statistics from DB
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.agents.completion_subscribers import (
    SubscriptionPersistenceError,
    remove_agent_completion_subscribers,
)
from gobby.agents.runtime_cleanup import AgentRuntimeCleanupResult
from gobby.events import CompletionEventRegistry
from gobby.events.wake import WakeDispatcher
from gobby.mcp_proxy.tools.agents import create_agents_registry
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.inter_session_messages import InterSessionMessageManager
from gobby.storage.pipeline_subscribers import CompletionSubscriberManager
from gobby.utils.session_context import session_context_for_test
from tests.completion_delivery_helpers import DeliveryRegistry, record_removals

pytestmark = pytest.mark.unit

_RUN_STARTED_AT = datetime(2026, 5, 20, tzinfo=UTC)
_RUN_COMPLETED_AT = datetime(2026, 5, 20, 0, 1, tzinfo=UTC)
_WAIT_RUN_ID = "796ce97e-38ee-508a-bdc0-f3ce2dded342"
_WAIT_ROOT_SESSION_ID = "12313230-63a9-5fd2-bdbb-f793325d2c16"
_WAIT_CALLER_SESSION_ID = "e3c98b06-11a5-5e52-9b82-b47a220be090"


def _make_mock_agent_run(
    run_id: str = "run-123",
    session_id: str | None = "sess-456",
    parent_session_id: str = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaa4001",
    status: str = "running",
    pid: int | None = None,
    provider: str = "claude",
    **kwargs: Any,
) -> MagicMock:
    """Create a mock AgentRun with to_dict() and to_brief() methods."""
    run = MagicMock()
    run.id = run_id
    run.child_session_id = session_id
    run.parent_session_id = parent_session_id
    run.status = status
    run.pid = pid
    run.provider = provider
    run.task_id = kwargs.get("task_id")
    run.started_at = kwargs.get("started_at")
    run.terminal_id = kwargs.get("terminal_id")
    run.worktree_id = kwargs.get("worktree_id")
    run.clone_id = kwargs.get("clone_id")
    run.workflow_name = kwargs.get("workflow_name")
    run.agent_name = kwargs.get("agent_name")
    run.model = kwargs.get("model")
    run.tool_calls_count = kwargs.get("tool_calls_count", 0)
    run.turns_used = kwargs.get("turns_used", 0)
    run.resume_metadata_json = kwargs.get("resume_metadata_json")

    run.to_dict.return_value = {
        "run_id": run_id,
        "id": run_id,
        "session_id": session_id,
        "parent_session_id": parent_session_id,
        "status": status,
        "pid": pid,
        "agent_name": kwargs.get("agent_name"),
        "workflow_name": kwargs.get("workflow_name"),
        "provider": provider,
        "model": kwargs.get("model"),
        "terminal_type": kwargs.get("terminal_type"),
        "worktree_id": kwargs.get("worktree_id"),
        "tool_calls_count": kwargs.get("tool_calls_count", 0),
        "turns_used": kwargs.get("turns_used", 0),
        "resume_metadata_json": kwargs.get("resume_metadata_json"),
    }
    run.to_brief.return_value = {
        "run_id": run_id,
        "session_id": session_id,
        "parent_session_id": parent_session_id,
        "pid": pid,
        "agent_name": kwargs.get("agent_name"),
        "workflow_name": kwargs.get("workflow_name"),
        "provider": provider,
        "model": kwargs.get("model"),
        "status": status,
    }
    return run


def _make_runner_with_run_storage() -> MagicMock:
    """Create a mock runner with a mock run_storage (LocalAgentRunManager)."""
    runner = MagicMock()
    runner.run_storage = MagicMock()
    runner.run_storage.list_active_global.return_value = []
    runner.run_storage.list_by_parent.return_value = []
    runner.run_storage.get.return_value = None
    runner.run_storage.get_by_session.return_value = None
    return runner


class TestCreateAgentsRegistry:
    """Tests for create_agents_registry factory function."""

    def test_creates_registry_with_correct_name(self) -> None:
        """Test registry has correct name."""
        runner = MagicMock()
        registry = create_agents_registry(runner)

        assert registry.name == "gobby-agents"
        assert "Agent" in registry.description

    def test_registers_all_expected_tools(self) -> None:
        """Test all agent tools are registered."""
        runner = MagicMock()
        registry = create_agents_registry(runner)

        expected_tools = [
            "spawn_agent",  # Unified spawn with isolation support
            "get_agent_result",
            "get_agent_capture",
            "wait_for_agent",
            "wait_for_output",
            "list_agent_runs",
            "stop_agent",
            "cancel_stale_helpers",
            "end_agent_run",
            "kill_agent",
            "can_spawn_agent",
            "list_running_agents",
            "get_running_agent",
            "unregister_agent",
            "running_agent_stats",
        ]

        for tool_name in expected_tools:
            assert registry.get_schema(tool_name) is not None, f"Missing tool: {tool_name}"

        assert registry.get_schema("list_agents") is None

    def test_kill_agent_schema_accepts_stop(self) -> None:
        """Regression: CLI kill -s sends stop through MCP validation."""
        runner = MagicMock()
        registry = create_agents_registry(runner)

        schema = registry.get_schema("kill_agent")

        assert schema is not None
        assert schema["inputSchema"]["properties"]["stop"]["type"] == "boolean"

    def test_agents_py_under_monolith_limit(self) -> None:
        """Guard against growing the non-test agents module into a monolith."""
        repo_root = Path(__file__).parents[3]
        agents_py = repo_root / "src/gobby/mcp_proxy/tools/agents.py"

        assert agents_py.read_text(encoding="utf-8").count("\n") < 1000


class TestGetAgentResult:
    """Tests for get_agent_result MCP tool."""

    @pytest.mark.asyncio
    async def test_run_not_found_returns_error(self) -> None:
        """Test error when run_id not found."""
        runner = MagicMock()
        runner.get_run.return_value = None

        registry = create_agents_registry(runner)
        get_result = registry._tools["get_agent_result"].func

        result = await get_result(run_id="non-existent")

        assert result["success"] is False
        assert "not found" in result["error"]

    @pytest.mark.asyncio
    async def test_returns_run_details(self) -> None:
        """Test successful run retrieval returns all details."""
        mock_run = MagicMock()
        mock_run.id = "run-123"
        mock_run.status = "success"
        mock_run.result = "Task completed"
        mock_run.error = None
        mock_run.provider = "claude"
        mock_run.model = "claude-3-opus"
        mock_run.prompt = "Do the thing"
        mock_run.tool_calls_count = 5
        mock_run.turns_used = 3
        mock_run.started_at = _RUN_STARTED_AT
        mock_run.completed_at = _RUN_COMPLETED_AT
        mock_run.child_session_id = "child-sess-456"

        runner = MagicMock()
        runner.get_run.return_value = mock_run

        registry = create_agents_registry(runner)
        get_result = registry._tools["get_agent_result"].func

        result = await get_result(run_id="run-123")

        assert result["success"] is True
        assert result["run_id"] == "run-123"
        assert result["status"] == "success"
        assert result["result"] == "Task completed"
        assert result["provider"] == "claude"
        assert result["model"] == "claude-3-opus"
        assert result["tool_calls_count"] == 5
        assert result["turns_used"] == 3
        assert result["child_session_id"] == "child-sess-456"


class TestWaitForAgent:
    """Tests for the subscribe-and-return wait_for_agent contract."""

    @staticmethod
    def _run(status: str = "running", *, result: str | None = None) -> MagicMock:
        run = MagicMock()
        run.id = _WAIT_RUN_ID
        run.status = status
        run.result = result
        run.error = None
        run.provider = "claude"
        run.model = "opus"
        run.prompt = "merge"
        run.tool_calls_count = 1
        run.turns_used = 1
        run.started_at = _RUN_STARTED_AT
        run.completed_at = _RUN_COMPLETED_AT if status == "success" else None
        run.child_session_id = "child-session"
        run.terminal_reason = None
        return run

    @staticmethod
    def _lineage_session_manager() -> MagicMock:
        session_manager = MagicMock()
        root = MagicMock(id=_WAIT_ROOT_SESSION_ID, parent_session_id=None)
        caller = MagicMock(
            id=_WAIT_CALLER_SESSION_ID,
            parent_session_id=_WAIT_ROOT_SESSION_ID,
        )
        sessions = {
            _WAIT_ROOT_SESSION_ID: root,
            _WAIT_CALLER_SESSION_ID: caller,
        }
        session_manager.get.side_effect = sessions.get
        return session_manager

    @staticmethod
    def _registry(
        runner: MagicMock,
        db: HubDatabase,
        completion_registry: CompletionEventRegistry,
        *,
        session_manager: Any | None = None,
    ) -> Any:
        return create_agents_registry(
            runner,
            session_manager=session_manager,
            db=db,
            completion_registry=completion_registry,
        )

    def test_public_signature_only_accepts_run_id(self) -> None:
        runner = MagicMock()
        registry = create_agents_registry(runner)
        wait_for_agent = registry._tools["wait_for_agent"].func

        assert list(inspect.signature(wait_for_agent).parameters) == ["run_id"]

    @pytest.mark.asyncio
    async def test_completed_run_returns_without_subscription(self) -> None:
        runner = MagicMock()
        runner.get_run.return_value = self._run("success", result="done")
        completion_registry = CompletionEventRegistry()
        registry = create_agents_registry(runner, completion_registry=completion_registry)

        result = await registry._tools["wait_for_agent"].func(_WAIT_RUN_ID)

        assert result["success"] is True
        assert result["completed"] is True
        assert result["notification_registered"] is False
        assert result["result"] == "done"
        assert not completion_registry.is_registered(_WAIT_RUN_ID)

    @pytest.mark.asyncio
    async def test_unknown_run_returns_error(self) -> None:
        runner = MagicMock()
        runner.get_run.return_value = None
        registry = create_agents_registry(runner)

        result = await registry._tools["wait_for_agent"].func(_WAIT_RUN_ID)

        assert result == {
            "success": False,
            "error": f"Agent run {_WAIT_RUN_ID} not found",
        }

    @pytest.mark.asyncio
    async def test_active_run_requires_session_context(self, temp_db: HubDatabase) -> None:
        runner = MagicMock()
        runner.get_run.return_value = self._run()
        registry = self._registry(runner, temp_db, CompletionEventRegistry())

        result = await registry._tools["wait_for_agent"].func(_WAIT_RUN_ID)

        assert result["success"] is False
        assert result["error_code"] == "missing_session_context"

    @pytest.mark.asyncio
    async def test_active_run_requires_completion_services(self) -> None:
        runner = MagicMock()
        runner.get_run.return_value = self._run()
        registry = create_agents_registry(runner)

        with session_context_for_test(_WAIT_CALLER_SESSION_ID):
            result = await registry._tools["wait_for_agent"].func(_WAIT_RUN_ID)

        assert result["success"] is False
        assert result["error_code"] == "completion_services_unavailable"

    @pytest.mark.asyncio
    async def test_persistence_failure_returns_structured_error(self, temp_db: HubDatabase) -> None:
        runner = MagicMock()
        runner.get_run.return_value = self._run()
        registry = self._registry(runner, temp_db, CompletionEventRegistry())

        with (
            session_context_for_test(_WAIT_CALLER_SESSION_ID),
            patch(
                "gobby.mcp_proxy.tools.agents_query_tools.subscribe_agent_completion",
                side_effect=SubscriptionPersistenceError("boom"),
            ),
        ):
            result = await registry._tools["wait_for_agent"].func(_WAIT_RUN_ID)

        assert result["success"] is False
        assert result["error_code"] == "subscription_persistence_failed"

    @pytest.mark.asyncio
    async def test_active_run_registers_durable_notification(self, temp_db: HubDatabase) -> None:
        runner = MagicMock()
        runner.get_run.return_value = self._run()
        completion_registry = CompletionEventRegistry()
        registry = self._registry(runner, temp_db, completion_registry)

        with session_context_for_test(_WAIT_CALLER_SESSION_ID):
            result = await registry._tools["wait_for_agent"].func(_WAIT_RUN_ID)

        assert result["success"] is True
        assert result["completed"] is False
        assert result["notification_registered"] is True
        assert result["notification_session_id"] == _WAIT_CALLER_SESSION_ID
        assert completion_registry.get_subscribers(_WAIT_RUN_ID) == [_WAIT_CALLER_SESSION_ID]
        assert CompletionSubscriberManager(temp_db).get_completion_subscribers(_WAIT_RUN_ID) == [
            _WAIT_CALLER_SESSION_ID
        ]

    @pytest.mark.asyncio
    async def test_repeated_active_wait_is_idempotent_and_quiet(
        self,
        temp_db: HubDatabase,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        runner = MagicMock()
        runner.get_run.return_value = self._run()
        completion_registry = CompletionEventRegistry()
        registry = self._registry(runner, temp_db, completion_registry)

        with (
            session_context_for_test(_WAIT_CALLER_SESSION_ID),
            caplog.at_level(logging.WARNING, logger="gobby.events.completion_registry"),
        ):
            first_result = await registry._tools["wait_for_agent"].func(_WAIT_RUN_ID)
            second_result = await registry._tools["wait_for_agent"].func(_WAIT_RUN_ID)

        assert first_result["success"] is True
        assert second_result["success"] is True
        assert completion_registry.get_subscribers(_WAIT_RUN_ID) == [_WAIT_CALLER_SESSION_ID]
        assert CompletionSubscriberManager(temp_db).get_completion_subscribers(_WAIT_RUN_ID) == [
            _WAIT_CALLER_SESSION_ID
        ]
        assert not [
            record
            for record in caplog.records
            if record.name == "gobby.events.completion_registry"
            and record.levelno >= logging.WARNING
        ]

    @pytest.mark.asyncio
    async def test_active_child_waiter_excludes_root_session(self, temp_db: HubDatabase) -> None:
        runner = MagicMock()
        runner.get_run.return_value = self._run()
        completion_registry = CompletionEventRegistry()
        registry = self._registry(
            runner,
            temp_db,
            completion_registry,
            session_manager=self._lineage_session_manager(),
        )

        with session_context_for_test(_WAIT_CALLER_SESSION_ID):
            result = await registry._tools["wait_for_agent"].func(_WAIT_RUN_ID)

        assert result["notification_registered"] is True
        assert completion_registry.get_subscribers(_WAIT_RUN_ID) == [_WAIT_CALLER_SESSION_ID]
        assert CompletionSubscriberManager(temp_db).get_completion_subscribers(_WAIT_RUN_ID) == [
            _WAIT_CALLER_SESSION_ID
        ]

    @pytest.mark.asyncio
    async def test_in_region_transition_cleans_only_fresh_owned_rows(
        self, temp_db: HubDatabase
    ) -> None:
        active = self._run()
        terminal = self._run("success", result="done")
        runner = MagicMock()
        runner.get_run.side_effect = [active, active, terminal]
        wake = AsyncMock()
        completion_registry = CompletionEventRegistry(wake_callback=wake)
        subscribers = CompletionSubscriberManager(temp_db)
        subscribers.add_completion_subscribers(_WAIT_RUN_ID, [_WAIT_ROOT_SESSION_ID])
        registry = self._registry(
            runner,
            temp_db,
            completion_registry,
        )

        with session_context_for_test(_WAIT_CALLER_SESSION_ID):
            result = await registry._tools["wait_for_agent"].func(_WAIT_RUN_ID)

        assert result["completed"] is True
        assert result["notification_registered"] is False
        assert subscribers.get_completion_subscribers(_WAIT_RUN_ID) == [_WAIT_ROOT_SESSION_ID]
        assert not completion_registry.is_registered(_WAIT_RUN_ID)
        await completion_registry.notify(_WAIT_RUN_ID, {"status": "success"})
        wake.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_in_region_transition_leaves_merged_entry_for_pending_notify(
        self, temp_db: HubDatabase
    ) -> None:
        active = self._run()
        terminal = self._run("success", result="done")
        runner = MagicMock()
        runner.get_run.side_effect = [active, active, terminal]
        wake = AsyncMock()
        completion_registry = CompletionEventRegistry(wake_callback=wake)
        completion_registry.register(_WAIT_RUN_ID, subscribers=[_WAIT_ROOT_SESSION_ID])
        subscribers = CompletionSubscriberManager(temp_db)
        subscribers.add_completion_subscribers(_WAIT_RUN_ID, [_WAIT_ROOT_SESSION_ID])
        registry = self._registry(
            runner,
            temp_db,
            completion_registry,
        )

        with session_context_for_test(_WAIT_CALLER_SESSION_ID):
            result = await registry._tools["wait_for_agent"].func(_WAIT_RUN_ID)

        assert result["completed"] is True
        assert completion_registry.get_subscribers(_WAIT_RUN_ID) == [
            _WAIT_ROOT_SESSION_ID,
            _WAIT_CALLER_SESSION_ID,
        ]
        assert subscribers.get_completion_subscribers(_WAIT_RUN_ID) == [
            _WAIT_ROOT_SESSION_ID,
            _WAIT_CALLER_SESSION_ID,
        ]

        await completion_registry.notify(_WAIT_RUN_ID, {"status": "success"})

        assert wake.await_count == 2
        wake.assert_any_await(_WAIT_ROOT_SESSION_ID, "", {"status": "success"})
        wake.assert_any_await(_WAIT_CALLER_SESSION_ID, "", {"status": "success"})

    @pytest.mark.asyncio
    async def test_late_notify_snapshot_does_not_gain_waiter(self, temp_db: HubDatabase) -> None:
        active = self._run()
        terminal = self._run("success", result="done")
        runner = MagicMock()
        runner.get_run.side_effect = [active, terminal]
        overlay_started = asyncio.Event()
        overlay_release = asyncio.Event()
        notify_snapshot_taken = asyncio.Event()
        notify_release = asyncio.Event()

        async def overlay(run: MagicMock, _reader: Any) -> MagicMock:
            if run is active:
                overlay_started.set()
                await overlay_release.wait()
            return run

        async def wake(_session_id: str, _message: str, _result: dict[str, Any]) -> None:
            notify_snapshot_taken.set()
            await notify_release.wait()

        completion_registry = CompletionEventRegistry(wake_callback=wake)
        completion_registry.register(_WAIT_RUN_ID, subscribers=[_WAIT_ROOT_SESSION_ID])
        subscribers = CompletionSubscriberManager(temp_db)
        subscribers.add_completion_subscribers(_WAIT_RUN_ID, [_WAIT_ROOT_SESSION_ID])
        registry = self._registry(runner, temp_db, completion_registry)

        async def notify_and_cleanup() -> None:
            await completion_registry.notify(_WAIT_RUN_ID, {"status": "success"})
            remove_agent_completion_subscribers(db=temp_db, run_id=_WAIT_RUN_ID)
            completion_registry.cleanup(_WAIT_RUN_ID)

        with (
            session_context_for_test(_WAIT_CALLER_SESSION_ID),
            patch(
                "gobby.mcp_proxy.tools.agents_query_tools.overlay_live_activity",
                side_effect=overlay,
            ),
        ):
            wait_task = asyncio.create_task(registry._tools["wait_for_agent"].func(_WAIT_RUN_ID))
            await overlay_started.wait()
            notify_task = asyncio.create_task(notify_and_cleanup())
            await notify_snapshot_taken.wait()
            overlay_release.set()
            result = await wait_task

        assert result["completed"] is True
        assert result["notification_registered"] is False
        assert completion_registry.get_subscribers(_WAIT_RUN_ID) == [_WAIT_ROOT_SESSION_ID]
        assert subscribers.get_completion_subscribers(_WAIT_RUN_ID) == [_WAIT_ROOT_SESSION_ID]
        notify_release.set()
        await notify_task
        assert subscribers.get_completion_subscribers(_WAIT_RUN_ID) == []

    @pytest.mark.asyncio
    async def test_completed_notify_retained_retry_row_is_untouched(
        self, temp_db: HubDatabase
    ) -> None:
        active = self._run()
        terminal = self._run("success", result="done")
        runner = MagicMock()
        runner.get_run.side_effect = [active, terminal]
        overlay_started = asyncio.Event()
        overlay_release = asyncio.Event()

        async def overlay(run: MagicMock, _reader: Any) -> MagicMock:
            if run is active:
                overlay_started.set()
                await overlay_release.wait()
            return run

        completion_registry = CompletionEventRegistry(wake_callback=AsyncMock())
        completion_registry.register(_WAIT_RUN_ID, subscribers=[_WAIT_ROOT_SESSION_ID])
        subscribers = CompletionSubscriberManager(temp_db)
        subscribers.add_completion_subscribers(
            _WAIT_RUN_ID,
            [_WAIT_ROOT_SESSION_ID, _WAIT_CALLER_SESSION_ID],
        )
        registry = self._registry(runner, temp_db, completion_registry)

        async def notify_and_retain_failed_delivery() -> None:
            await completion_registry.notify(_WAIT_RUN_ID, {"status": "success"})
            remove_agent_completion_subscribers(
                db=temp_db,
                run_id=_WAIT_RUN_ID,
                session_ids=[_WAIT_ROOT_SESSION_ID],
            )
            completion_registry.cleanup(_WAIT_RUN_ID)

        with (
            session_context_for_test(_WAIT_CALLER_SESSION_ID),
            patch(
                "gobby.mcp_proxy.tools.agents_query_tools.overlay_live_activity",
                side_effect=overlay,
            ),
        ):
            wait_task = asyncio.create_task(registry._tools["wait_for_agent"].func(_WAIT_RUN_ID))
            await overlay_started.wait()
            await notify_and_retain_failed_delivery()
            overlay_release.set()
            result = await wait_task

        assert result["completed"] is True
        assert result["notification_registered"] is False
        assert subscribers.get_completion_subscribers(_WAIT_RUN_ID) == [_WAIT_CALLER_SESSION_ID]


class TestListAgentRuns:
    """Tests for list_agent_runs MCP tool."""

    @pytest.mark.asyncio
    async def test_returns_empty_list_when_no_runs(self) -> None:
        """Test empty list when no runs exist."""
        runner = MagicMock()
        runner.list_runs.return_value = []

        registry = create_agents_registry(runner)
        list_agent_runs = registry._tools["list_agent_runs"].func

        result = list_agent_runs(parent_session_id="sess-123")

        assert result["success"] is True
        assert result["runs"] == []
        assert result["count"] == 0

    @pytest.mark.asyncio
    async def test_returns_compact_decision_fields(self) -> None:
        """List rows expose only identity and coordinator decision fields."""
        mock_run = MagicMock()
        mock_run.id = "run-123"
        mock_run.status = "running"
        mock_run.agent_name = "backend-developer"
        mock_run.started_at = _RUN_STARTED_AT
        mock_run.tool_calls_count = 4
        mock_run.turns_used = 2
        mock_run.resume_metadata_json = {
            "task_ref": "#18213",
            "branch_name": "fix/18213-agents-list-get",
            "sandbox_args": ["workspace-write"],
        }

        runner = MagicMock()
        runner.list_runs.return_value = [mock_run]

        registry = create_agents_registry(runner)
        list_agent_runs = registry._tools["list_agent_runs"].func

        result = list_agent_runs(parent_session_id="sess-123")

        assert result["success"] is True
        assert result["count"] == 1
        assert result["runs"][0] == {
            "run_id": "run-123",
            "task_ref": "#18213",
            "agent_name": "backend-developer",
            "status": "running",
            "started_at": _RUN_STARTED_AT,
            "branch_name": "fix/18213-agents-list-get",
            "tool_calls_count": 4,
            "turns_used": 2,
        }

    @pytest.mark.asyncio
    async def test_respects_status_filter(self) -> None:
        """Test status filter is passed to runner."""
        runner = MagicMock()
        runner.list_runs.return_value = []

        registry = create_agents_registry(runner)
        list_agent_runs = registry._tools["list_agent_runs"].func

        list_agent_runs(parent_session_id="sess-123", status="running")

        runner.list_runs.assert_called_once_with("sess-123", status="running", limit=20)
        assert runner.list_runs.call_count == 1
        assert runner.list_runs.call_args is not None

    @pytest.mark.asyncio
    async def test_respects_limit(self) -> None:
        """Test limit parameter is passed to runner."""
        runner = MagicMock()
        runner.list_runs.return_value = []

        registry = create_agents_registry(runner)
        list_agent_runs = registry._tools["list_agent_runs"].func

        list_agent_runs(parent_session_id="sess-123", limit=50)

        runner.list_runs.assert_called_once_with("sess-123", status=None, limit=50)
        assert runner.list_runs.call_count == 1
        assert runner.list_runs.call_args is not None

    @pytest.mark.asyncio
    async def test_clamps_zero_limit_to_positive_bound(self) -> None:
        """Explicit zero is clamped to the smallest positive limit."""
        runner = MagicMock()
        runner.list_runs.return_value = []

        registry = create_agents_registry(runner)
        list_agent_runs = registry._tools["list_agent_runs"].func

        result = list_agent_runs(parent_session_id="sess-123", limit=0)

        assert result["success"] is True
        runner.list_runs.assert_called_once_with("sess-123", status=None, limit=1)


class TestStopAgent:
    """Tests for stop_agent MCP tool."""

    @pytest.mark.asyncio
    async def test_successful_stop(self) -> None:
        """Test successful agent stop."""
        runner = _make_runner_with_run_storage()
        runner.get_run.return_value = _make_mock_agent_run(status="running")
        runner.cancel_run.return_value = True
        runtime_db = MagicMock(spec=HubDatabase)

        registry = create_agents_registry(runner, db=runtime_db)
        stop_agent = registry._tools["stop_agent"].func

        with (
            patch(
                "gobby.mcp_proxy.tools.agents._kill_agent_process",
                new_callable=AsyncMock,
                return_value={
                    "success": False,
                    "error": "No target PID found",
                    "error_code": "no_target_pid",
                },
            ),
            patch(
                "gobby.mcp_proxy.tools.agents.cleanup_agent_runtime_state",
                return_value=AgentRuntimeCleanupResult(
                    dispatch_mutex_rows=1,
                    workflow_instance_rows=1,
                ),
            ) as cleanup,
            patch(
                "gobby.agents.terminal_delivery.deliver_existing_terminal_run_in_scope",
                new_callable=AsyncMock,
                return_value=True,
            ),
        ):
            result = await stop_agent(run_id="run-123")

        assert result["success"] is True
        assert "stopped" in result["message"]
        assert result["terminal_reason"] == "user_cancelled"
        assert result["agent_step_instances_deleted"] == 1
        assert "workflow_instances_deleted" not in result
        runner.cancel_run.assert_called_once_with("run-123")
        cleanup.assert_called_once_with(
            runtime_db,
            run_id="run-123",
            child_session_id="sess-456",
            terminal_reason="user_cancelled",
        )

    @pytest.mark.asyncio
    async def test_successful_stop_passes_task_manager_to_cancellation_helper(self) -> None:
        """Test stop_agent wires task recovery dependencies into fallback cancellation."""
        runner = _make_runner_with_run_storage()
        runner.get_run.return_value = _make_mock_agent_run(status="running")
        task_manager = MagicMock()

        registry = create_agents_registry(runner, task_manager=task_manager)
        stop_agent = registry._tools["stop_agent"].func

        with (
            patch(
                "gobby.mcp_proxy.tools.agents._kill_agent_process",
                new_callable=AsyncMock,
                return_value={"success": True},
            ) as kill_process,
            patch(
                "gobby.mcp_proxy.tools.agent_cancellation.terminalize_cancelled_agent_run",
                new_callable=AsyncMock,
                return_value=True,
            ) as terminalize,
            patch("gobby.mcp_proxy.tools.agents.cleanup_agent_runtime_state"),
        ):
            result = await stop_agent(run_id="run-123")

        assert result["success"] is True
        assert result["run_id"] == "run-123"
        assert result["status"] == "cancelled"
        assert result["terminal_reason"] == "user_cancelled"
        runner.get_run.assert_called_once_with("run-123")
        kill_process.assert_awaited_once()
        assert kill_process.call_args.args[0] is runner.get_run.return_value
        assert kill_process.call_args.kwargs["signal_name"] == "TERM"
        assert kill_process.call_args.kwargs["close_terminal"] is True
        terminalize.assert_awaited_once_with(
            runner=runner,
            run_id="run-123",
            terminal_reason="user_cancelled",
            lifecycle_monitor=None,
            completion_registry=None,
            task_manager=task_manager,
            message="Agent run-123 cancelled",
        )

    @pytest.mark.asyncio
    async def test_run_not_found(self) -> None:
        """Test error when run not found."""
        runner = MagicMock()
        runner.cancel_run.return_value = False
        runner.get_run.return_value = None

        registry = create_agents_registry(runner)
        stop_agent = registry._tools["stop_agent"].func

        result = await stop_agent(run_id="non-existent")

        assert result["success"] is False
        assert "not found" in result["error"]

    @pytest.mark.asyncio
    async def test_cannot_stop_completed_run(self) -> None:
        """Test error when trying to stop non-running agent."""
        mock_run = MagicMock()
        mock_run.status = "success"

        runner = MagicMock()
        runner.cancel_run.return_value = False
        runner.get_run.return_value = mock_run

        registry = create_agents_registry(runner)
        stop_agent = registry._tools["stop_agent"].func

        result = await stop_agent(run_id="run-123")

        assert result["success"] is False
        assert "Cannot stop" in result["error"]
        assert "success" in result["error"]


class TestCanSpawnAgent:
    """Tests for can_spawn_agent MCP tool."""

    @pytest.mark.asyncio
    async def test_can_spawn_returns_true(self) -> None:
        """Test when spawning is allowed."""
        runner = MagicMock()
        runner.can_spawn.return_value = (True, "Spawning allowed", 0)

        registry = create_agents_registry(runner)
        can_spawn = registry._tools["can_spawn_agent"].func

        result = can_spawn(parent_session_id="sess-123")

        assert result["can_spawn"] is True
        assert result["reason"] == "Spawning allowed"

    @pytest.mark.asyncio
    async def test_cannot_spawn_returns_false(self) -> None:
        """Test when spawning is not allowed."""
        runner = MagicMock()
        runner.can_spawn.return_value = (False, "Max depth reached", 3)

        registry = create_agents_registry(runner)
        can_spawn = registry._tools["can_spawn_agent"].func

        result = can_spawn(parent_session_id="sess-123")

        assert result["can_spawn"] is False
        assert result["reason"] == "Max depth reached"


class TestListRunningAgents:
    """Tests for list_running_agents MCP tool (DB-backed via LocalAgentRunManager)."""

    def _make_agents(self) -> list[MagicMock]:
        """Create test agents."""
        return [
            _make_mock_agent_run(
                run_id="run-1",
                session_id="sess-1",
                parent_session_id="parent-1",
                pid=1001,
            ),
            _make_mock_agent_run(
                run_id="run-2",
                session_id="sess-2",
                parent_session_id="parent-1",
                pid=1002,
            ),
            _make_mock_agent_run(
                run_id="run-3",
                session_id="sess-3",
                parent_session_id="parent-2",
                pid=1003,
            ),
        ]

    @pytest.mark.asyncio
    async def test_list_all_running_agents(self) -> None:
        """Test listing all running agents."""
        runner = _make_runner_with_run_storage()
        agents = self._make_agents()
        runner.run_storage.list_active_global.return_value = agents

        registry = create_agents_registry(runner)
        list_running = registry._tools["list_running_agents"].func

        result = await list_running()

        assert result["success"] is True
        assert result["count"] == 3
        assert len(result["agents"]) == 3
        assert result["scope"] == "all"
        runner.run_storage.list_active_global.assert_called_once_with(limit=100)

    @pytest.mark.asyncio
    async def test_list_rows_have_only_compact_decision_fields(self) -> None:
        """Running-agent rows omit full record and resume state fields."""
        runner = _make_runner_with_run_storage()
        run = _make_mock_agent_run(
            run_id="run-1",
            task_id="task-uuid",
            agent_name="backend-developer",
            started_at=_RUN_STARTED_AT,
            tool_calls_count=7,
            turns_used=3,
            resume_metadata_json={
                "task_ref": "#18213",
                "branch_name": "fix/18213-agents-list-get",
                "sandbox_args": ["workspace-write"],
                "tmux_config": {"window": "agent"},
                "config_overrides": ["model=x"],
            },
        )
        runner.run_storage.list_active_global.return_value = [run]

        registry = create_agents_registry(runner)
        result = await registry._tools["list_running_agents"].func()

        assert result["agents"][0] == {
            "run_id": "run-1",
            "task_ref": "#18213",
            "agent_name": "backend-developer",
            "status": "running",
            "started_at": _RUN_STARTED_AT,
            "branch_name": "fix/18213-agents-list-get",
            "tool_calls_count": 7,
            "turns_used": 3,
        }

    @pytest.mark.asyncio
    async def test_clamps_zero_limit_to_positive_bound(self) -> None:
        """Explicit zero is clamped to the smallest positive limit."""
        runner = _make_runner_with_run_storage()
        runner.run_storage.list_active_global.return_value = []

        registry = create_agents_registry(runner)
        list_running = registry._tools["list_running_agents"].func

        result = await list_running(limit=0)

        assert result["success"] is True
        assert result["count"] == 0
        runner.run_storage.list_active_global.assert_called_once_with(limit=1)

    @pytest.mark.asyncio
    async def test_filter_by_parent_session(self) -> None:
        """Test filtering by parent session ID."""
        runner = _make_runner_with_run_storage()
        agents = self._make_agents()
        parent1_agents = [a for a in agents if a.parent_session_id == "parent-1"]
        runner.run_storage.list_by_parent.return_value = parent1_agents

        registry = create_agents_registry(runner)
        list_running = registry._tools["list_running_agents"].func

        result = await list_running(parent_session_id="parent-1")

        assert result["success"] is True
        assert result["count"] == 2
        runner.run_storage.list_by_parent.assert_called_once_with(
            "parent-1",
            limit=100,
        )

    @pytest.mark.asyncio
    async def test_default_all_scope_ignores_current_session_context(self) -> None:
        """Default listing is build-wide even when MCP seeds caller session context."""
        from gobby.utils.session_context import session_context_for_test

        runner = _make_runner_with_run_storage()
        agents = self._make_agents()
        runner.run_storage.list_active_global.return_value = agents

        registry = create_agents_registry(runner)
        list_running = registry._tools["list_running_agents"].func

        with session_context_for_test("caller-session"):
            result = await list_running()

        assert result["success"] is True
        assert result["count"] == 3
        runner.run_storage.list_active_global.assert_called_once_with(limit=100)
        runner.run_storage.list_by_parent.assert_not_called()

    @pytest.mark.asyncio
    async def test_parent_scope_uses_current_session_context_when_requested(self) -> None:
        """Callers can still ask for direct children of the current session."""
        from gobby.utils.session_context import session_context_for_test

        runner = _make_runner_with_run_storage()
        parent_agents = [a for a in self._make_agents() if a.parent_session_id == "parent-1"]
        runner.run_storage.list_by_parent.return_value = parent_agents

        registry = create_agents_registry(runner)
        list_running = registry._tools["list_running_agents"].func

        with session_context_for_test("parent-1"):
            result = await list_running(scope="parent")

        assert result["success"] is True
        assert result["count"] == 2
        assert result["scope"] == "parent"
        runner.run_storage.list_by_parent.assert_called_once_with(
            "parent-1",
            limit=100,
        )

    @pytest.mark.asyncio
    async def test_running_status_uses_cli_equivalent_query(self) -> None:
        """status='running' uses the same storage path as CLI --status running."""
        runner = _make_runner_with_run_storage()
        running_agents = [self._make_agents()[0]]
        runner.run_storage.list_running.return_value = running_agents

        registry = create_agents_registry(runner)
        list_running = registry._tools["list_running_agents"].func

        result = await list_running(status="running", limit=25)

        assert result["success"] is True
        assert result["count"] == 1
        assert result["status"] == "running"
        runner.run_storage.list_running.assert_called_once_with(limit=25)

    @pytest.mark.asyncio
    async def test_list_includes_agent_identity(self) -> None:
        """List payloads expose agent identity so orchestrators can filter workers."""
        runner = _make_runner_with_run_storage()
        runner.run_storage.list_by_parent.return_value = [
            _make_mock_agent_run(
                run_id="run-worker",
                session_id="sess-worker",
                parent_session_id="parent-1",
                agent_name="merge-worker",
                workflow_name="merge-worker",
                model="sonnet",
            )
        ]

        registry = create_agents_registry(runner)
        list_running = registry._tools["list_running_agents"].func

        result = await list_running(parent_session_id="parent-1")

        assert result["agents"][0]["agent_name"] == "merge-worker"
        assert "workflow_name" not in result["agents"][0]
        assert "model" not in result["agents"][0]


class TestGetRunningAgent:
    """Tests for get_running_agent MCP tool (DB-backed)."""

    RUN_ID = "11111111-1111-4111-8111-111111111111"

    @pytest.mark.asyncio
    async def test_agent_found(self) -> None:
        """Test getting an existing running agent."""
        runner = _make_runner_with_run_storage()
        mock_run = _make_mock_agent_run(
            run_id=self.RUN_ID,
            session_id="sess-456",
            parent_session_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaa4001",
            pid=12345,
            provider="claude",
            status="running",
        )
        runner.run_storage.get.return_value = mock_run

        registry = create_agents_registry(runner)
        get_running = registry._tools["get_running_agent"].func

        result = await get_running(run_id=self.RUN_ID)

        assert result["success"] is True
        assert result["agent"]["run_id"] == self.RUN_ID
        assert result["agent"]["pid"] == 12345
        assert result["agent"]["session_id"] == "sess-456"
        assert "resume_metadata_json" not in result["agent"]

    @pytest.mark.asyncio
    async def test_agent_not_found(self) -> None:
        """Test error when agent not found."""
        runner = _make_runner_with_run_storage()
        runner.run_storage.get.return_value = None

        registry = create_agents_registry(runner)
        get_running = registry._tools["get_running_agent"].func

        result = await get_running(run_id="22222222-2222-4222-8222-222222222222")

        assert result["success"] is False
        assert "no running agent found" in result["error"].lower()

    @pytest.mark.asyncio
    async def test_completed_agent_not_returned(self) -> None:
        """Test that completed agents are not returned as 'running'."""
        runner = _make_runner_with_run_storage()
        mock_run = _make_mock_agent_run(run_id=self.RUN_ID, status="success")
        runner.run_storage.get.return_value = mock_run

        registry = create_agents_registry(runner)
        get_running = registry._tools["get_running_agent"].func

        result = await get_running(run_id=self.RUN_ID)

        assert result["success"] is False
        assert "no running agent found" in result["error"].lower()

    @pytest.mark.asyncio
    async def test_resume_metadata_requires_explicit_opt_in(self) -> None:
        runner = _make_runner_with_run_storage()
        run = _make_mock_agent_run(
            run_id=self.RUN_ID,
            resume_metadata_json={"sandbox_args": ["workspace-write"]},
        )
        runner.run_storage.get.return_value = run
        get_running = create_agents_registry(runner)._tools["get_running_agent"].func

        result = await get_running(run_id=self.RUN_ID, include_resume_metadata=True)

        assert result["agent"]["resume_metadata_json"] == {"sandbox_args": ["workspace-write"]}

    @pytest.mark.asyncio
    async def test_unique_short_prefix_resolves_run(self) -> None:
        runner = _make_runner_with_run_storage()
        run = _make_mock_agent_run(run_id=self.RUN_ID)
        runner.run_storage.find_by_id_prefix.return_value = [run]
        get_running = create_agents_registry(runner)._tools["get_running_agent"].func

        result = await get_running(run_id="11111111")

        assert result["success"] is True
        assert result["agent"]["run_id"] == self.RUN_ID
        runner.run_storage.find_by_id_prefix.assert_called_once_with("11111111", limit=2)
        runner.run_storage.get.assert_not_called()

    @pytest.mark.asyncio
    async def test_ambiguous_prefix_returns_structured_invalid_arguments(self) -> None:
        runner = _make_runner_with_run_storage()
        runner.run_storage.find_by_id_prefix.return_value = [
            _make_mock_agent_run(run_id=self.RUN_ID),
            _make_mock_agent_run(run_id="11111111-2222-4222-8222-222222222222"),
        ]
        get_running = create_agents_registry(runner)._tools["get_running_agent"].func

        result = await get_running(run_id="11111111")

        assert result["success"] is False
        assert result["error_code"] == "INVALID_ARGUMENTS"
        assert "postgres" not in result["error"].lower()

    @pytest.mark.asyncio
    async def test_malformed_run_id_returns_structured_invalid_arguments(self) -> None:
        runner = _make_runner_with_run_storage()
        get_running = create_agents_registry(runner)._tools["get_running_agent"].func

        result = await get_running(run_id="not-a-run-id")

        assert result["success"] is False
        assert result["error_code"] == "INVALID_ARGUMENTS"
        runner.run_storage.get.assert_not_called()
        runner.run_storage.find_by_id_prefix.assert_not_called()


class TestUnregisterAgent:
    """Tests for unregister_agent MCP tool (DB-backed via agent_run_manager.fail)."""

    @pytest.mark.asyncio
    async def test_successful_unregistration(self) -> None:
        """Test successful agent unregistration (marks the run as cancelled)."""
        runner = _make_runner_with_run_storage()
        mock_run = _make_mock_agent_run(run_id="run-123", status="running")
        runner.run_storage.get.return_value = mock_run
        runner.cancel_run.return_value = True

        registry = create_agents_registry(runner)
        unregister = registry._tools["unregister_agent"].func

        result = await unregister(run_id="run-123")

        assert result["success"] is True
        assert "Unregistered" in result["message"]
        runner.cancel_run.assert_called_once_with("run-123")

    @pytest.mark.asyncio
    async def test_unregister_not_found(self) -> None:
        """Test error when agent not found."""
        runner = _make_runner_with_run_storage()
        runner.run_storage.get.return_value = None

        registry = create_agents_registry(runner)
        unregister = registry._tools["unregister_agent"].func

        result = await unregister(run_id="non-existent")

        assert result["success"] is False
        assert "no agent found" in result["error"].lower()

    @pytest.mark.asyncio
    async def test_unregister_already_completed(self) -> None:
        """Test unregistering an already-completed agent returns success with message."""
        runner = _make_runner_with_run_storage()
        mock_run = _make_mock_agent_run(run_id="run-123", status="success")
        runner.run_storage.get.return_value = mock_run

        registry = create_agents_registry(runner)
        unregister = registry._tools["unregister_agent"].func

        result = await unregister(run_id="run-123")

        assert result["success"] is True
        assert "already in status" in result["message"]
        runner.run_storage.fail.assert_not_called()


class TestKillAgent:
    """Tests for kill_agent MCP tool."""

    @pytest.mark.asyncio
    async def test_requires_run_id_or_session_id(self) -> None:
        """Test error when neither run_id nor session_id provided."""
        runner = _make_runner_with_run_storage()

        registry = create_agents_registry(runner)
        kill_agent = registry._tools["kill_agent"].func

        result = await kill_agent()

        assert result["success"] is False
        assert "run_id or session_id required" in result["error"]

    @pytest.mark.asyncio
    async def test_invalid_signal_rejected(self) -> None:
        """Test invalid signal is rejected."""
        runner = _make_runner_with_run_storage()

        registry = create_agents_registry(runner)
        kill_agent = registry._tools["kill_agent"].func

        result = await kill_agent(run_id="run-123", signal="INVALID")

        assert result["success"] is False
        assert "Invalid signal" in result["error"]

    @pytest.mark.asyncio
    async def test_invalid_status_rejected(self) -> None:
        """Only terminal agent statuses are accepted."""
        runner = _make_runner_with_run_storage()

        registry = create_agents_registry(runner)
        kill_agent = registry._tools["kill_agent"].func

        result = await kill_agent(run_id="run-123", status="running")

        assert result["success"] is False
        assert "Invalid status" in result["error"]
        runner.get_run.assert_not_called()

    @pytest.mark.asyncio
    async def test_session_id_resolves_to_run_id(self) -> None:
        """Test that session_id resolves to run_id via DB."""
        runner = _make_runner_with_run_storage()
        mock_run = _make_mock_agent_run(
            run_id="run-123",
            session_id="sess-456",
            parent_session_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaa4001",
        )
        runner.run_storage.get_by_session.return_value = mock_run
        runner.get_run.return_value = mock_run

        registry = create_agents_registry(runner)
        kill_agent = registry._tools["kill_agent"].func

        with patch(
            "gobby.mcp_proxy.tools.agents._kill_agent_process",
            new_callable=AsyncMock,
            return_value={"success": True},
        ):
            result = await kill_agent(session_id="sess-456")

        # The key assertion: it found the agent via session_id
        assert "No agent found for session" not in result.get("error", "")

    @pytest.mark.asyncio
    async def test_parent_kill_by_session_id_defaults_to_cancelled(self) -> None:
        runner = _make_runner_with_run_storage()
        mock_run = _make_mock_agent_run(
            run_id="run-123",
            session_id="sess-456",
            parent_session_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaa4001",
        )
        runner.run_storage.get_by_session.return_value = mock_run
        runner.get_run.return_value = mock_run
        runner.cancel_run.return_value = True

        registry = create_agents_registry(runner)
        kill_agent = registry._tools["kill_agent"].func

        from gobby.utils.session_context import session_context_for_test

        with (
            session_context_for_test("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaa4001"),
            patch(
                "gobby.mcp_proxy.tools.agents._kill_agent_process",
                new_callable=AsyncMock,
                return_value={
                    "success": False,
                    "error": "No target PID found",
                    "error_code": "no_target_pid",
                },
            ),
        ):
            result = await kill_agent(session_id="sess-456")

        assert result["status"] == "cancelled"
        runner.cancel_run.assert_called_once_with("run-123")
        runner.complete_run.assert_not_called()

    @pytest.mark.asyncio
    async def test_session_id_not_found_returns_error(self) -> None:
        """Test error when session_id doesn't match any agent."""
        runner = _make_runner_with_run_storage()
        runner.run_storage.get_by_session.return_value = None
        runner.get_run_id_by_session.return_value = None

        registry = create_agents_registry(runner)
        kill_agent = registry._tools["kill_agent"].func

        result = await kill_agent(session_id="non-existent")

        assert result["success"] is False
        assert "No agent found for session" in result["error"]

    @pytest.mark.asyncio
    async def test_default_full_cleanup(self) -> None:
        """Test that kill_agent does full cleanup by default."""
        runner = _make_runner_with_run_storage()
        mock_run = _make_mock_agent_run(
            run_id="run-123",
            session_id="sess-456",
            parent_session_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaa4001",
        )
        runner.get_run.return_value = mock_run
        runner.cancel_run.return_value = True

        registry = create_agents_registry(runner)
        kill_agent = registry._tools["kill_agent"].func

        with patch(
            "gobby.mcp_proxy.tools.agents._kill_agent_process",
            new_callable=AsyncMock,
            return_value={"success": True},
        ):
            result = await kill_agent(run_id="run-123")

        assert result["success"] is True
        assert result["workflow_stopped"] is True
        runner.cancel_run.assert_called_once_with("run-123")

    @pytest.mark.asyncio
    async def test_parent_kill_forwards_terminal_services(self) -> None:
        """A parent kill must hand kill.py the services that reach capture.py.

        Without terminal_services, _close_tmux_session short-circuits before
        terminate_managed_runtime_async and _close_terminal_window raw-kills the
        process group, losing the final transcript capture.
        """
        runner = _make_runner_with_run_storage()
        mock_run = _make_mock_agent_run(
            run_id="run-123",
            session_id="sess-456",
            parent_session_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaa4001",
        )
        runner.get_run.return_value = mock_run
        runner.cancel_run.return_value = True
        services = object()
        runner.terminal_services = services

        registry = create_agents_registry(runner)
        kill_agent = registry._tools["kill_agent"].func

        with patch(
            "gobby.mcp_proxy.tools.agents._kill_agent_process",
            new_callable=AsyncMock,
            return_value={"success": True},
        ) as kill_process:
            result = await kill_agent(run_id="run-123")

        assert result["success"] is True
        assert kill_process.call_args.kwargs["close_terminal"] is True
        assert kill_process.call_args.kwargs["terminal_services"] is services

    @pytest.mark.asyncio
    async def test_stop_false_kills_without_terminalizing_workflow(self) -> None:
        """CLI kill without --stop should not cancel workflow state."""
        runner = _make_runner_with_run_storage()
        mock_run = _make_mock_agent_run(
            run_id="run-123",
            session_id="sess-456",
            parent_session_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaa4001",
        )
        runner.get_run.return_value = mock_run

        registry = create_agents_registry(runner)
        kill_agent = registry._tools["kill_agent"].func

        with (
            patch(
                "gobby.mcp_proxy.tools.agents._kill_agent_process",
                new_callable=AsyncMock,
                return_value={"success": True},
            ) as kill_process,
            patch(
                "gobby.mcp_proxy.tools.agents._cleanup_terminal_artifacts",
                new_callable=AsyncMock,
            ) as cleanup,
        ):
            result = await kill_agent(run_id="run-123", stop=False)

        assert result["success"] is True
        assert result["workflow_stopped"] is False
        assert kill_process.call_args.kwargs["close_terminal"] is True
        cleanup.assert_not_awaited()
        runner.cancel_run.assert_not_called()
        runner.complete_run.assert_not_called()
        runner.run_storage.fail.assert_not_called()

    @pytest.mark.asyncio
    async def test_debug_preserves_state(self) -> None:
        """Test that debug=True preserves workflow state."""
        runner = _make_runner_with_run_storage()
        mock_run = _make_mock_agent_run(
            run_id="run-123",
            session_id="sess-456",
            parent_session_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaa4001",
        )
        runner.get_run.return_value = mock_run
        runner.cancel_run.return_value = True

        registry = create_agents_registry(runner)
        kill_agent = registry._tools["kill_agent"].func

        with patch(
            "gobby.mcp_proxy.tools.agents._kill_agent_process",
            new_callable=AsyncMock,
            return_value={"success": True},
        ):
            result = await kill_agent(run_id="run-123", debug=True)

        assert result["success"] is True


class TestEndAgentRun:
    """Tests for end_agent_run MCP tool."""

    @pytest.mark.asyncio
    async def test_requires_active_session_context(self) -> None:
        runner = _make_runner_with_run_storage()
        registry = create_agents_registry(runner)

        result = await registry._tools["end_agent_run"].func()

        assert result["success"] is False
        assert "No active session context" in result["error"]

    @pytest.mark.asyncio
    async def test_resolves_current_session_and_notifies_success(self) -> None:
        runner = _make_runner_with_run_storage()
        mock_run = _make_mock_agent_run(
            run_id="run-123",
            session_id="sess-456",
            parent_session_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaa4001",
        )
        runner.run_storage.get_by_session.return_value = mock_run
        runner.get_run.return_value = _make_mock_agent_run(
            run_id="run-123",
            session_id="sess-456",
            parent_session_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaa4001",
            status="success",
        )
        runner.complete_run.return_value = True
        completion_registry = MagicMock()
        completion_registry.get_result.return_value = None
        completion_registry.notify = AsyncMock()

        registry = create_agents_registry(runner, completion_registry=completion_registry)

        from gobby.utils.session_context import session_context_for_test

        with (
            session_context_for_test("sess-456"),
            patch(
                "gobby.mcp_proxy.tools.agents._kill_agent_process",
                new_callable=AsyncMock,
                return_value={"success": True},
            ),
        ):
            result = await registry._tools["end_agent_run"].func()

        assert result == {"success": True, "run_id": "run-123", "status": "success"}
        runner.complete_run.assert_called_once_with("run-123", result=None)
        completion_registry.notify.assert_awaited_once_with(
            "run-123",
            result={"status": "success", "run_id": "run-123"},
            message="Agent run-123 completed",
        )

    @pytest.mark.asyncio
    async def test_db_backed_registry_terminalizes_without_evidence_lookup(
        self, temp_db: HubDatabase
    ) -> None:
        runner = _make_runner_with_run_storage()
        mock_run = _make_mock_agent_run(
            run_id="run-123",
            session_id="sess-456",
            parent_session_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaa4001",
            status="success",
        )
        runner.run_storage.get_by_session.return_value = mock_run
        runner.get_run.return_value = mock_run
        runner.complete_run.return_value = True

        from gobby.utils.session_context import session_context_for_test

        with (
            patch(
                "gobby.mcp_proxy.tools.agents_registry.LocalAgentRunManager",
                return_value=runner.run_storage,
            ),
            session_context_for_test("sess-456"),
            patch(
                "gobby.mcp_proxy.tools.agents._kill_agent_process",
                new_callable=AsyncMock,
                return_value={"success": True},
            ),
        ):
            registry = create_agents_registry(runner, db=temp_db)
            result = await registry._tools["end_agent_run"].func()

        assert result == {"success": True, "run_id": "run-123", "status": "success"}

    @pytest.mark.asyncio
    async def test_kills_provider_before_terminalizing_run(self) -> None:
        runner = _make_runner_with_run_storage()
        mock_run = _make_mock_agent_run(
            run_id="run-123",
            session_id="sess-456",
            parent_session_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaa4001",
        )
        runner.run_storage.get_by_session.return_value = mock_run
        runner.get_run.return_value = mock_run
        events: list[tuple[str, str, bool | None]] = []

        def complete_run(run_id: str, result: str | None = None) -> bool:
            events.append(("complete", run_id, None))
            return True

        async def kill_process(*args: Any, **kwargs: Any) -> dict[str, bool]:
            run = args[0]
            events.append(("kill", run.id, kwargs.get("close_terminal")))
            return {"success": True}

        runner.complete_run.side_effect = complete_run
        registry = create_agents_registry(runner)

        from gobby.utils.session_context import session_context_for_test

        with (
            session_context_for_test("sess-456"),
            patch(
                "gobby.mcp_proxy.tools.agents._kill_agent_process",
                new_callable=AsyncMock,
                side_effect=kill_process,
            ),
        ):
            result = await registry._tools["end_agent_run"].func()

        assert result == {"success": True, "run_id": "run-123", "status": "success"}
        assert events == [("kill", "run-123", False), ("complete", "run-123", None)]

    @pytest.mark.asyncio
    async def test_returns_error_when_session_has_no_agent_run(self) -> None:
        runner = _make_runner_with_run_storage()
        runner.get_run_id_by_session.return_value = None
        registry = create_agents_registry(runner)

        from gobby.utils.session_context import session_context_for_test

        with session_context_for_test("sess-456"):
            result = await registry._tools["end_agent_run"].func()

        assert result["success"] is False
        assert "No agent found for session sess-456" == result["error"]

    @pytest.mark.asyncio
    async def test_unsubscribed_memory_helper_end_agent_run_does_not_notify_parent(
        self, temp_db: HubDatabase
    ) -> None:
        runner = _make_runner_with_run_storage()
        mock_run = _make_mock_agent_run(
            run_id="run-123",
            session_id="sess-456",
            parent_session_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaa4001",
        )
        runner.run_storage.get_by_session.return_value = mock_run
        runner.get_run.return_value = mock_run
        runner.complete_run.return_value = True

        ism_manager = InterSessionMessageManager(temp_db)
        session_manager = MagicMock()
        session_manager.get.return_value = MagicMock(
            id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaa4001", agent_depth=0
        )
        wake_dispatcher = WakeDispatcher(session_manager, ism_manager)
        completion_registry = CompletionEventRegistry(wake_callback=wake_dispatcher.wake)
        registry = create_agents_registry(
            runner,
            completion_registry=completion_registry,
            session_manager=session_manager,
        )

        from gobby.utils.session_context import session_context_for_test

        with (
            session_context_for_test("sess-456"),
            patch(
                "gobby.mcp_proxy.tools.agents._kill_agent_process",
                new_callable=AsyncMock,
                return_value={"success": True},
            ),
        ):
            result = await registry._tools["end_agent_run"].func()

        assert result == {"success": True, "run_id": "run-123", "status": "success"}
        assert ism_manager.list_messages("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaa4001") == []
        assert not ism_manager.has_completion_notification(
            "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaa4001",
            "completion_notification",
            "run-123",
        )


class TestKillAgentSelfTerminationViaRunId:
    """Tests for self-termination detection via run_id path using _context."""

    @pytest.mark.asyncio
    async def test_run_id_self_termination_defaults_to_success(self) -> None:
        """When agent calls kill_agent(run_id=...) and _context matches, default to success."""
        runner = _make_runner_with_run_storage()
        mock_run = _make_mock_agent_run(
            run_id="run-123",
            session_id="sess-456",
            parent_session_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaa4001",
        )
        runner.get_run.return_value = mock_run
        runner.complete_run.return_value = True

        registry = create_agents_registry(runner)
        kill_agent = registry._tools["kill_agent"].func

        # Set session context matching the agent's session (self-termination)
        from gobby.utils.session_context import session_context_for_test

        with (
            session_context_for_test("sess-456"),
            patch(
                "gobby.mcp_proxy.tools.agents._kill_agent_process",
                new_callable=AsyncMock,
                return_value={"success": True},
            ),
        ):
            result = await kill_agent(run_id="run-123")

        assert result["success"] is True
        # Should call complete_run (success), not cancel_run (cancelled)
        runner.complete_run.assert_called_once_with("run-123", result=None)
        runner.cancel_run.assert_not_called()

    @pytest.mark.asyncio
    async def test_db_backed_run_id_self_termination_defaults_to_success(
        self, temp_db: HubDatabase
    ) -> None:
        runner = _make_runner_with_run_storage()
        mock_run = _make_mock_agent_run(
            run_id="run-123",
            session_id="sess-456",
            parent_session_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaa4001",
        )
        runner.get_run.return_value = mock_run
        runner.complete_run.return_value = True
        registry = create_agents_registry(runner, db=temp_db)
        kill_agent = registry._tools["kill_agent"].func

        from gobby.utils.session_context import session_context_for_test

        with (
            session_context_for_test("sess-456"),
            patch(
                "gobby.mcp_proxy.tools.agents._kill_agent_process",
                new_callable=AsyncMock,
                return_value={"success": True},
            ),
        ):
            result = await kill_agent(run_id="run-123")

        assert result["success"] is True
        runner.complete_run.assert_called_once_with("run-123", result=None)

    @pytest.mark.asyncio
    async def test_run_id_parent_kill_defaults_to_cancelled(self) -> None:
        """When parent kills agent via run_id, session context doesn't match, default to cancelled."""
        runner = _make_runner_with_run_storage()
        mock_run = _make_mock_agent_run(
            run_id="run-123",
            session_id="sess-456",
            parent_session_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaa4001",
        )
        runner.get_run.return_value = mock_run
        runner.cancel_run.return_value = True

        registry = create_agents_registry(runner)
        kill_agent = registry._tools["kill_agent"].func

        # Set session context with different session_id (parent killing child)
        from gobby.utils.session_context import session_context_for_test

        with (
            session_context_for_test("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaa4001"),
            patch(
                "gobby.mcp_proxy.tools.agents._kill_agent_process",
                new_callable=AsyncMock,
                return_value={"success": True},
            ),
        ):
            result = await kill_agent(run_id="run-123")

        assert result["success"] is True
        runner.cancel_run.assert_called_once_with("run-123")
        runner.complete_run.assert_not_called()

    @pytest.mark.asyncio
    async def test_run_id_no_context_defaults_to_cancelled(self) -> None:
        """Without _context, run_id path defaults to cancelled (backward compat)."""
        runner = _make_runner_with_run_storage()
        mock_run = _make_mock_agent_run(
            run_id="run-123",
            session_id="sess-456",
            parent_session_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaa4001",
        )
        runner.get_run.return_value = mock_run
        runner.cancel_run.return_value = True

        registry = create_agents_registry(runner)
        kill_agent = registry._tools["kill_agent"].func

        with patch(
            "gobby.mcp_proxy.tools.agents._kill_agent_process",
            new_callable=AsyncMock,
            return_value={"success": True},
        ):
            result = await kill_agent(run_id="run-123")

        assert result["success"] is True
        runner.cancel_run.assert_called_once_with("run-123")
        runner.complete_run.assert_not_called()


class TestRunningAgentStats:
    """Tests for running_agent_stats MCP tool (DB-backed)."""

    @pytest.mark.asyncio
    async def test_empty_stats(self) -> None:
        """Test stats with no running agents."""
        runner = _make_runner_with_run_storage()
        runner.run_storage.list_active_global.return_value = []

        registry = create_agents_registry(runner)
        stats = registry._tools["running_agent_stats"].func

        result = stats()

        assert result["success"] is True
        assert result["total"] == 0
        assert result["by_parent_count"] == 0

    @pytest.mark.asyncio
    async def test_stats_with_agents(self) -> None:
        """Test stats with multiple running agents."""
        runner = _make_runner_with_run_storage()
        runner.run_storage.list_active_global.return_value = [
            _make_mock_agent_run(
                run_id="run-1",
                parent_session_id="parent-1",
            ),
            _make_mock_agent_run(
                run_id="run-2",
                parent_session_id="parent-1",
            ),
            _make_mock_agent_run(
                run_id="run-3",
                parent_session_id="parent-2",
            ),
            _make_mock_agent_run(
                run_id="run-4",
                parent_session_id="parent-3",
            ),
        ]

        registry = create_agents_registry(runner)
        stats = registry._tools["running_agent_stats"].func

        result = stats()

        assert result["success"] is True
        assert result["total"] == 4
        assert result["by_parent_count"] == 3  # 3 unique parents


class TestTerminalCleanupHookIsolation:
    """Terminal cleanup must not fabricate lifecycle hook events."""

    def test_shared_cleanup_has_no_hook_rule_dispatch(self) -> None:
        import inspect

        from gobby.mcp_proxy.tools import agents_termination

        source = inspect.getsource(agents_termination)

        assert "_fire_synthetic_stop" not in source
        assert "evaluate_workflow_rules" not in source
        assert "HookEventType.STOP" not in source
        assert "HookEventType.SESSION_END" not in source


class TestCompleteSelfTerminatedRunSignoffMessage:
    """`_complete_self_terminated_run` reads the agent's adversary_verdict
    session var and threads it via signoff_message in the notify_result dict
    so the wake dispatcher's content-fallback chain surfaces the verdict to
    the parent's P2P inbox.
    """

    @pytest.mark.asyncio
    async def test_provider_process_is_killed_before_terminal_persistence(self) -> None:
        from gobby.mcp_proxy.tools.agents import _complete_self_terminated_run

        events: list[str] = []
        run = MagicMock(
            id="run-ordered-cleanup",
            child_session_id=None,
            terminal_id=None,
        )
        runner = MagicMock()

        async def kill_process(*_args: object, **_kwargs: object) -> dict[str, bool]:
            events.append("kill")
            return {"success": True}

        async def complete_run(*_args: object, **_kwargs: object) -> bool:
            events.append("complete")
            return True

        with (
            patch(
                "gobby.mcp_proxy.tools.agents._kill_agent_process",
                new=kill_process,
            ),
            patch(
                "gobby.mcp_proxy.tools.agents.complete_and_notify_agent_run",
                new=complete_run,
            ),
            patch(
                "gobby.mcp_proxy.tools.agents._cleanup_terminal_artifacts",
                new_callable=AsyncMock,
            ),
        ):
            result = await _complete_self_terminated_run(
                runner=runner,
                run=run,
                kill_db=MagicMock(),
                completion_registry=MagicMock(),
                session_manager=None,
            )

        assert events == ["kill", "complete"]
        assert result["success"] is True

    @pytest.mark.asyncio
    async def test_self_termination_removes_acknowledged_subscription(
        self,
        temp_db: HubDatabase,
    ) -> None:
        from gobby.mcp_proxy.tools.agents import _complete_self_terminated_run

        run = _make_mock_agent_run(
            run_id=_WAIT_RUN_ID,
            session_id=None,
            status="running",
        )
        run.terminal_id = None
        terminal_run = _make_mock_agent_run(
            run_id=_WAIT_RUN_ID,
            session_id=None,
            status="success",
        )
        runner = MagicMock()
        runner.run_storage.db = temp_db
        runner.complete_run.return_value = True
        runner.get_run.return_value = terminal_run
        wake_callback = AsyncMock(return_value={"ism_persisted": True})
        completion_registry = CompletionEventRegistry(wake_callback=wake_callback)
        completion_registry.register(_WAIT_RUN_ID, subscribers=[_WAIT_ROOT_SESSION_ID])
        subscribers = CompletionSubscriberManager(temp_db)
        subscribers.add_completion_subscribers(_WAIT_RUN_ID, [_WAIT_ROOT_SESSION_ID])

        with (
            patch(
                "gobby.mcp_proxy.tools.agents._kill_agent_process",
                new_callable=AsyncMock,
                return_value={"success": True},
            ),
            patch(
                "gobby.mcp_proxy.tools.agents._cleanup_terminal_artifacts",
                new_callable=AsyncMock,
            ),
        ):
            result = await _complete_self_terminated_run(
                runner=runner,
                run=run,
                kill_db=temp_db,
                completion_registry=completion_registry,
                session_manager=None,
            )

        assert result["success"] is True
        assert subscribers.get_completion_subscribers(_WAIT_RUN_ID) == []
        assert not completion_registry.is_registered(_WAIT_RUN_ID)
        wake_callback.assert_awaited_once()
        assert wake_callback.call_args.args[2]["run_id"] == _WAIT_RUN_ID

    @pytest.mark.asyncio
    async def test_signoff_message_set_when_adversary_verdict_present(self) -> None:
        from gobby.mcp_proxy.tools.agents import _complete_self_terminated_run

        run = MagicMock()
        run.id = "run-xyz"
        run.child_session_id = "child-sess-1"
        run.terminal_id = None
        runner = MagicMock()
        kill_db = MagicMock()
        completion_registry = MagicMock()
        completion_registry.get_result = MagicMock(return_value=None)
        completion_registry.notify = AsyncMock()

        with (
            patch(
                "gobby.mcp_proxy.tools.agents._kill_agent_process",
                new_callable=AsyncMock,
                return_value={
                    "success": False,
                    "error": "No target PID found",
                    "error_code": "no_target_pid",
                },
            ),
            patch(
                "gobby.mcp_proxy.tools.agents.complete_and_notify_agent_run",
                new_callable=AsyncMock,
                return_value=True,
            ) as mock_complete,
            patch(
                "gobby.mcp_proxy.tools.agents._cleanup_terminal_artifacts",
                new_callable=AsyncMock,
            ),
            patch("gobby.workflows.state_manager.SessionVariableManager") as svm_cls,
        ):
            svm_cls.return_value.get_variables.return_value = {
                "adversary_verdict": "REJECTED: round 5, 1 blocking (sample)"
            }
            result = await _complete_self_terminated_run(
                runner=runner,
                run=run,
                kill_db=kill_db,
                completion_registry=completion_registry,
                session_manager=None,
            )

        notify_result = mock_complete.call_args.kwargs["notify_result"]
        assert notify_result["signoff_message"] == "REJECTED: round 5, 1 blocking (sample)"
        assert notify_result["status"] == "success"
        assert notify_result["run_id"] == "run-xyz"
        assert result["error_code"] == "no_target_pid"
        assert "terminal_cleanup_error" not in result

    @pytest.mark.asyncio
    async def test_signoff_message_omitted_when_var_unset(self) -> None:
        """Non-adversary runs leave adversary_verdict unset; the result dict
        must NOT carry a signoff_message so the existing fallback message wins.
        """
        from gobby.mcp_proxy.tools.agents import _complete_self_terminated_run

        run = MagicMock()
        run.id = "run-abc"
        run.child_session_id = "child-sess-2"
        run.terminal_id = None
        runner = MagicMock()
        kill_db = MagicMock()

        with (
            patch(
                "gobby.mcp_proxy.tools.agents._kill_agent_process",
                new_callable=AsyncMock,
                return_value={"success": True},
            ),
            patch(
                "gobby.mcp_proxy.tools.agents.complete_and_notify_agent_run",
                new_callable=AsyncMock,
                return_value=True,
            ) as mock_complete,
            patch(
                "gobby.mcp_proxy.tools.agents._cleanup_terminal_artifacts",
                new_callable=AsyncMock,
            ),
            patch("gobby.workflows.state_manager.SessionVariableManager") as svm_cls,
        ):
            svm_cls.return_value.get_variables.return_value = {}  # var unset
            await _complete_self_terminated_run(
                runner=runner,
                run=run,
                kill_db=kill_db,
                completion_registry=None,
                session_manager=None,
            )

        notify_result = mock_complete.call_args.kwargs["notify_result"]
        assert "signoff_message" not in notify_result
        assert notify_result["status"] == "success"
        assert notify_result["run_id"] == "run-abc"


class TestKillAgentCapturePreemptedDelivery:
    """Plan 1.4.7: kill_agent delivers capture-committed rows for both stop values."""

    def _harness(self, delivery: dict[str, bool] | None = None) -> tuple[Any, Any]:
        from contextlib import nullcontext
        from types import SimpleNamespace

        runner = _make_runner_with_run_storage()
        mock_run = _make_mock_agent_run(run_id="run-123")
        runner.get_run.return_value = mock_run
        runner.run_storage.db.bounded_transaction.return_value = nullcontext()
        runner.run_storage.get.return_value = SimpleNamespace(
            id="run-123", status="cancelled", error=None
        )
        registry = DeliveryRegistry(delivery or {"waiter-sess": True})
        tool_registry = create_agents_registry(runner, completion_registry=registry)
        return tool_registry._tools["kill_agent"].func, registry

    @pytest.mark.asyncio
    @pytest.mark.parametrize("stop", [True, False])
    async def test_kill_failure_after_close_still_delivers(
        self, monkeypatch: pytest.MonkeyPatch, stop: bool
    ) -> None:
        kill_agent, registry = self._harness()
        removals = record_removals(monkeypatch)
        kill_result = {
            "success": False,
            "error": "Terminal closed but no target PID was found to verify process death",
            "error_code": "kill_verification_failed",
        }
        with patch(
            "gobby.mcp_proxy.tools.agents._kill_agent_process",
            new_callable=AsyncMock,
            return_value=dict(kill_result),
        ):
            result = await kill_agent(run_id="run-123", stop=stop)

        assert result == kill_result
        assert len(registry.notify_calls) == 1
        assert removals == [("run-123", ["waiter-sess"])]
        assert registry.cleanup_calls == ["run-123"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("stop", [True, False])
    async def test_kill_raises_after_commit_delivers_before_propagation(
        self, monkeypatch: pytest.MonkeyPatch, stop: bool
    ) -> None:
        kill_agent, registry = self._harness()
        removals = record_removals(monkeypatch)
        with (
            patch(
                "gobby.mcp_proxy.tools.agents._kill_agent_process",
                new_callable=AsyncMock,
                side_effect=RuntimeError("kill exploded after committing"),
            ),
            pytest.raises(RuntimeError, match="kill exploded"),
        ):
            await kill_agent(run_id="run-123", stop=stop)

        assert len(registry.notify_calls) == 1
        assert removals == [("run-123", ["waiter-sess"])]
        assert registry.cleanup_calls == ["run-123"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("stop", [True, False])
    async def test_commits_then_cancel_settles_before_cancelled_error(
        self, monkeypatch: pytest.MonkeyPatch, stop: bool
    ) -> None:
        kill_agent, registry = self._harness()
        removals = record_removals(monkeypatch)
        started = asyncio.Event()
        release = asyncio.Event()

        async def _gated_kill(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
            started.set()
            await release.wait()
            return {
                "success": False,
                "error": "kill interrupted",
                "error_code": "kill_verification_failed",
            }

        with patch(
            "gobby.mcp_proxy.tools.agents._kill_agent_process",
            new_callable=AsyncMock,
            side_effect=_gated_kill,
        ):
            task = asyncio.ensure_future(kill_agent(run_id="run-123", stop=stop))
            await started.wait()
            task.cancel()
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await task

        assert len(registry.notify_calls) == 1
        assert removals == [("run-123", ["waiter-sess"])]
        assert registry.cleanup_calls == ["run-123"]

    @pytest.mark.asyncio
    async def test_stop_false_delivers_without_explicit_terminalize(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        kill_agent, registry = self._harness()
        removals = record_removals(monkeypatch)
        with (
            patch(
                "gobby.mcp_proxy.tools.agents._kill_agent_process",
                new_callable=AsyncMock,
                return_value={"success": True},
            ),
            patch(
                "gobby.mcp_proxy.tools.agents_lifecycle_tools.terminalize_killed_agent_run",
                new_callable=AsyncMock,
            ) as terminalize,
        ):
            result = await kill_agent(run_id="run-123", stop=False)

        assert result["workflow_stopped"] is False
        terminalize.assert_not_awaited()
        assert len(registry.notify_calls) == 1
        assert removals == [("run-123", ["waiter-sess"])]
        assert registry.cleanup_calls == ["run-123"]


_PREFIX_RUN_ID = "11111111-1111-4111-8111-111111111111"
_OTHER_PREFIX_RUN_ID = "11111111-2222-4222-8222-222222222222"


def _make_terminal_run(run_id: str) -> MagicMock:
    run = _make_mock_agent_run(run_id=run_id, status="success")
    run.result = "done"
    run.error = None
    run.prompt = "prompt"
    run.completed_at = None
    run.terminal_reason = None
    return run


@pytest.mark.asyncio
async def test_get_agent_result_resolves_unique_prefix() -> None:
    """A unique hexadecimal prefix resolves like the CLI does (#21097)."""
    runner = _make_runner_with_run_storage()
    runner.run_storage.find_by_id_prefix.return_value = [_make_terminal_run(_PREFIX_RUN_ID)]
    get_result = create_agents_registry(runner)._tools["get_agent_result"].func

    result = await get_result(run_id="11111111")

    assert result["success"] is True
    assert result["run_id"] == _PREFIX_RUN_ID
    assert result["status"] == "success"
    runner.run_storage.find_by_id_prefix.assert_called_once_with("11111111", limit=2)
    runner.get_run.assert_not_called()


@pytest.mark.asyncio
async def test_get_agent_result_reports_ambiguous_prefix() -> None:
    """An ambiguous prefix names its candidates instead of leaking a storage error."""
    runner = _make_runner_with_run_storage()
    runner.run_storage.find_by_id_prefix.return_value = [
        _make_terminal_run(_PREFIX_RUN_ID),
        _make_terminal_run(_OTHER_PREFIX_RUN_ID),
    ]
    get_result = create_agents_registry(runner)._tools["get_agent_result"].func

    result = await get_result(run_id="11111111")

    assert result["success"] is False
    assert result["error_code"] == "INVALID_ARGUMENTS"
    assert "11111111" in result["error"]
    assert result["matches"] == [_PREFIX_RUN_ID, _OTHER_PREFIX_RUN_ID]
    runner.get_run.assert_not_called()


@pytest.mark.asyncio
async def test_wait_for_agent_resolves_unique_prefix_to_terminal_run() -> None:
    """wait_for_agent accepts the same prefix and reports the resolved full id."""
    runner = _make_runner_with_run_storage()
    runner.run_storage.find_by_id_prefix.return_value = [_make_terminal_run(_PREFIX_RUN_ID)]
    wait = create_agents_registry(runner)._tools["wait_for_agent"].func

    result = await wait(run_id="11111111")

    assert result["success"] is True
    assert result["completed"] is True
    assert result["run_id"] == _PREFIX_RUN_ID
    runner.get_run.assert_not_called()


@pytest.mark.asyncio
async def test_full_uuid_keeps_the_exact_match_path() -> None:
    runner = _make_runner_with_run_storage()
    runner.get_run.return_value = _make_terminal_run(_PREFIX_RUN_ID)
    get_result = create_agents_registry(runner)._tools["get_agent_result"].func

    result = await get_result(run_id=_PREFIX_RUN_ID)

    assert result["run_id"] == _PREFIX_RUN_ID
    runner.get_run.assert_called_once_with(_PREFIX_RUN_ID)
    runner.run_storage.find_by_id_prefix.assert_not_called()
