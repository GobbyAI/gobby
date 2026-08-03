"""Regression tests for cancelling background pipeline executions."""

import asyncio
from collections.abc import Generator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.mcp_proxy.tools.workflows._pipeline_execution import (
    _background_tasks,
    _background_tasks_by_execution,
    _register_background_task,
    cancel_pipeline,
)
from gobby.workflows.pipeline_state import ExecutionStatus, StepStatus

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _clear_background_tasks() -> Generator[None]:
    _background_tasks.clear()
    _background_tasks_by_execution.clear()
    yield
    _background_tasks.clear()
    _background_tasks_by_execution.clear()


def _execution_manager() -> tuple[MagicMock, MagicMock]:
    manager = MagicMock()
    manager.db = MagicMock()
    execution = MagicMock()
    execution.id = "execution-123"
    execution.pipeline_name = "deploy"
    execution.session_id = "caller-session"
    execution.status = ExecutionStatus.RUNNING
    manager.get_execution.return_value = execution
    return manager, execution


@pytest.mark.asyncio
async def test_cancel_pipeline_stops_exact_background_task_and_preserves_cancelled_status() -> None:
    manager, _execution = _execution_manager()
    running_step = MagicMock(id="step-execution-1", status=StepStatus.RUNNING)
    manager.get_steps_for_execution.return_value = [running_step]
    blocker = asyncio.Event()
    background_task = asyncio.create_task(blocker.wait())
    _register_background_task("execution-123", background_task)

    with (
        patch("gobby.storage.sessions.SessionManager") as session_manager_cls,
        patch("gobby.storage.agents.LocalAgentRunManager"),
    ):
        session_manager_cls.return_value.find_active_by_external_id.return_value = None
        result = await cancel_pipeline(manager, "execution-123")

    assert result["success"] is True
    assert background_task.cancelled()
    assert "execution-123" not in _background_tasks_by_execution
    manager.update_step_execution.assert_called_once_with(
        step_execution_id="step-execution-1",
        status=StepStatus.CANCELLED,
    )
    manager.update_execution_status.assert_called_once_with(
        execution_id="execution-123",
        status=ExecutionStatus.CANCELLED,
    )


@pytest.mark.asyncio
async def test_cancel_pipeline_kills_only_pipeline_child_session_agents() -> None:
    manager, _execution = _execution_manager()
    manager.get_steps_for_execution.return_value = []
    pipeline_run = MagicMock(id="pipeline-run")
    unrelated_caller_run = MagicMock(id="caller-run")

    with (
        patch("gobby.storage.sessions.SessionManager") as session_manager_cls,
        patch("gobby.storage.agents.LocalAgentRunManager") as agent_manager_cls,
        patch("gobby.agents.kill.kill_agent", new_callable=AsyncMock) as kill_agent,
    ):
        pipeline_session = MagicMock(id="pipeline-session")
        session_manager_cls.return_value.find_active_by_external_id.return_value = pipeline_session
        agent_manager_cls.return_value.list_by_parent.return_value = [pipeline_run]

        result = await cancel_pipeline(manager, "execution-123")

    assert result["success"] is True
    session_manager_cls.return_value.find_active_by_external_id.assert_called_once_with(
        "pipeline-execution-123",
        "pipeline",
    )
    agent_manager_cls.return_value.list_by_parent.assert_called_once_with("pipeline-session")
    kill_agent.assert_awaited_once_with(pipeline_run, manager.db, signal_name="KILL")
    assert kill_agent.await_args.args[0] is not unrelated_caller_run
