"""Regression tests for cancelling background pipeline executions."""

import asyncio
import logging
from collections.abc import Generator
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from gobby.hub.caller_scope import bind_caller, reset_caller
from gobby.mcp_proxy.tools.workflows._pipeline_execution import (
    _background_tasks,
    _background_tasks_by_execution,
    _register_background_task,
    cancel_pipeline,
)
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.machines import LocalMachineManager
from gobby.workflows.pipeline_state import ExecutionStatus, StepStatus
from tests.fixtures.postgres import TEST_USER_ID
from tests.servers.conftest import OTHER_USER_ID

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


def _bound_manager(temp_db: HubDatabase, steps: list[MagicMock]) -> MagicMock:
    manager, _execution = _execution_manager()
    manager.db = temp_db
    manager.get_steps_for_execution.return_value = steps
    return manager


def _owned_machine(temp_db: HubDatabase, owner_user_id: str) -> str:
    temp_db.execute(
        """
        INSERT INTO users (id, email, name, password_hash)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (id) DO NOTHING
        """,
        (owner_user_id, f"{owner_user_id}@example.com", "Owner", "not-a-password"),
    )
    machine_id = str(uuid4())
    LocalMachineManager(temp_db).upsert_seen(machine_id, owner_user_id)
    return machine_id


def _pending_background_task() -> asyncio.Task[None]:
    blocker = asyncio.Event()

    async def blocked() -> None:
        await blocker.wait()

    background_task = asyncio.create_task(blocked())
    _register_background_task("execution-123", background_task)
    return background_task


async def _release(background_task: asyncio.Task[None]) -> None:
    background_task.cancel()
    await asyncio.gather(background_task, return_exceptions=True)


def _assert_no_cancel_effects(manager: MagicMock, background_task: asyncio.Task[None]) -> None:
    assert not background_task.done()
    assert "execution-123" in _background_tasks_by_execution
    manager.update_step_execution.assert_not_called()
    manager.update_execution_status.assert_not_called()


@pytest.mark.asyncio
async def test_cancel_pipeline_stops_exact_background_task_and_preserves_cancelled_status() -> None:
    manager, _execution = _execution_manager()
    running_step = MagicMock(id="step-execution-1", status=StepStatus.RUNNING)
    manager.get_steps_for_execution.return_value = [running_step]
    blocker = asyncio.Event()

    async def blocked() -> None:
        await blocker.wait()

    background_task = asyncio.create_task(blocked())
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
    assert kill_agent.await_args is not None
    assert kill_agent.await_args.args[0] is not unrelated_caller_run


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize("pipeline_child_present", [True, False])
async def test_cancel_pipeline_refuses_unowned_pipeline_before_any_change(
    temp_db: HubDatabase,
    pipeline_child_present: bool,
) -> None:
    owned_by_user = _owned_machine(temp_db, TEST_USER_ID)
    manager = _bound_manager(
        temp_db,
        [
            MagicMock(id="step-1", status=StepStatus.RUNNING),
            MagicMock(id="step-2", status=StepStatus.PENDING),
        ],
    )
    background_task = _pending_background_task()
    pipeline_child = (
        SimpleNamespace(id="pipeline-session", machine_id=owned_by_user)
        if pipeline_child_present
        else None
    )

    token = bind_caller(OTHER_USER_ID, temp_db)
    try:
        with (
            patch("gobby.storage.sessions.SessionManager") as session_manager_cls,
            patch("gobby.storage.agents.LocalAgentRunManager") as agent_manager_cls,
            patch("gobby.agents.kill.kill_agent", new_callable=AsyncMock) as kill_agent,
        ):
            session_manager_cls.return_value.get.return_value = SimpleNamespace(
                machine_id=owned_by_user
            )
            session_manager_cls.return_value.find_active_by_external_id.return_value = (
                pipeline_child
            )
            agent_manager_cls.return_value.list_by_parent.return_value = [
                MagicMock(id="pipeline-run", machine_id=owned_by_user)
            ]
            result = await cancel_pipeline(manager, "execution-123")

        assert result["success"] is False
        assert result["error_code"] == "machine_not_owned"
        assert "status" not in result
        kill_agent.assert_not_awaited()
        _assert_no_cancel_effects(manager, background_task)
    finally:
        reset_caller(token)
        await _release(background_task)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_cancel_pipeline_refuses_unowned_target_before_any_change(
    temp_db: HubDatabase,
) -> None:
    caller_machine = _owned_machine(temp_db, OTHER_USER_ID)
    foreign_machine = _owned_machine(temp_db, TEST_USER_ID)
    manager = _bound_manager(temp_db, [MagicMock(id="step-1", status=StepStatus.RUNNING)])
    background_task = _pending_background_task()
    owned_run = MagicMock(id="owned-run", machine_id=caller_machine)
    foreign_run = MagicMock(id="foreign-run", machine_id=foreign_machine)

    token = bind_caller(OTHER_USER_ID, temp_db)
    try:
        with (
            patch("gobby.storage.sessions.SessionManager") as session_manager_cls,
            patch("gobby.storage.agents.LocalAgentRunManager") as agent_manager_cls,
            patch("gobby.agents.kill.kill_agent", new_callable=AsyncMock) as kill_agent,
        ):
            session_manager_cls.return_value.get.return_value = SimpleNamespace(
                machine_id=caller_machine
            )
            session_manager_cls.return_value.find_active_by_external_id.return_value = (
                SimpleNamespace(id="pipeline-session", machine_id=caller_machine)
            )
            agent_manager_cls.return_value.list_by_parent.return_value = [owned_run, foreign_run]
            result = await cancel_pipeline(manager, "execution-123")

        assert result["success"] is False
        assert result["error_code"] == "machine_not_owned"
        kill_agent.assert_not_awaited()
        _assert_no_cancel_effects(manager, background_task)
    finally:
        reset_caller(token)
        await _release(background_task)


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize("session_id", [None, "caller-session"])
async def test_cancel_pipeline_refuses_missing_owner_before_any_change(
    temp_db: HubDatabase,
    session_id: str | None,
) -> None:
    machine = _owned_machine(temp_db, OTHER_USER_ID)
    manager = _bound_manager(temp_db, [MagicMock(id="step-1", status=StepStatus.RUNNING)])
    manager.get_execution.return_value.session_id = session_id
    background_task = _pending_background_task()

    token = bind_caller(OTHER_USER_ID, temp_db)
    try:
        with (
            patch("gobby.storage.sessions.SessionManager") as session_manager_cls,
            patch("gobby.storage.agents.LocalAgentRunManager") as agent_manager_cls,
            patch("gobby.agents.kill.kill_agent", new_callable=AsyncMock) as kill_agent,
        ):
            session_manager_cls.return_value.get.return_value = None
            session_manager_cls.return_value.find_active_by_external_id.return_value = (
                SimpleNamespace(id="pipeline-session", machine_id=machine)
            )
            agent_manager_cls.return_value.list_by_parent.return_value = [
                MagicMock(id="pipeline-run", machine_id=machine)
            ]
            result = await cancel_pipeline(manager, "execution-123")

        assert result["success"] is False
        assert result["error_code"] == "machine_not_found"
        kill_agent.assert_not_awaited()
        _assert_no_cancel_effects(manager, background_task)
    finally:
        reset_caller(token)
        await _release(background_task)


@pytest.mark.asyncio
async def test_cancel_pipeline_unbound_caller_preserves_internal_cancel() -> None:
    manager, execution = _execution_manager()
    execution.session_id = None
    manager.get_steps_for_execution.return_value = []
    background_task = _pending_background_task()
    pipeline_run = MagicMock(id="pipeline-run")

    try:
        with (
            patch("gobby.storage.sessions.SessionManager") as session_manager_cls,
            patch("gobby.storage.agents.LocalAgentRunManager") as agent_manager_cls,
            patch(
                "gobby.agents.kill.kill_agent",
                new_callable=AsyncMock,
                return_value={"success": True},
            ) as kill_agent,
        ):
            session_manager_cls.return_value.find_active_by_external_id.return_value = (
                SimpleNamespace(id="pipeline-session")
            )
            agent_manager_cls.return_value.list_by_parent.return_value = [pipeline_run]
            result = await cancel_pipeline(manager, "execution-123")

        assert result["success"] is True
        assert result["status"] == "cancelled"
        assert background_task.cancelled()
        manager.update_execution_status.assert_called_once_with(
            execution_id="execution-123",
            status=ExecutionStatus.CANCELLED,
        )
        kill_agent.assert_awaited_once_with(pipeline_run, manager.db, signal_name="KILL")
    finally:
        await _release(background_task)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_cancel_pipeline_does_not_count_a_refused_kill(
    temp_db: HubDatabase,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO)
    caller_machine = _owned_machine(temp_db, OTHER_USER_ID)
    foreign_machine = _owned_machine(temp_db, TEST_USER_ID)
    manager = _bound_manager(temp_db, [MagicMock(id="step-1", status=StepStatus.RUNNING)])
    background_task = _pending_background_task()
    foreign_run = MagicMock(id="foreign-run", machine_id=foreign_machine)

    token = bind_caller(OTHER_USER_ID, temp_db)
    try:
        with (
            patch("gobby.storage.sessions.SessionManager") as session_manager_cls,
            patch("gobby.storage.agents.LocalAgentRunManager") as agent_manager_cls,
            patch(
                "gobby.agents.kill.kill_agent",
                new_callable=AsyncMock,
                return_value={"success": False, "error_code": "machine_not_owned"},
            ) as kill_agent,
        ):
            session_manager_cls.return_value.get.return_value = SimpleNamespace(
                machine_id=caller_machine
            )
            session_manager_cls.return_value.find_active_by_external_id.return_value = (
                SimpleNamespace(id="pipeline-session", machine_id=caller_machine)
            )
            agent_manager_cls.return_value.list_by_parent.return_value = [foreign_run]
            result = await cancel_pipeline(manager, "execution-123")

        assert result["success"] is False
        assert result["error_code"] == "machine_not_owned"
        kill_agent.assert_not_awaited()
        _assert_no_cancel_effects(manager, background_task)
        assert "Killed 1 agents" not in caplog.text
    finally:
        reset_caller(token)
        await _release(background_task)
