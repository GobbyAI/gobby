"""The task_active guard lets a team manager resume its own seat while its own run is active."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.agents.isolation import NoneIsolationHandler
from gobby.agents.spawn import PreparedSpawn
from gobby.mcp_proxy.tools.spawn_agent import (
    _factory,
    _implementation,
    _spawn_guards,
    create_spawn_agent_registry,
)
from gobby.storage.agents import LocalAgentRunManager
from gobby.storage.session_models import Session
from gobby.storage.sessions import SessionManager
from gobby.utils.machine_id import require_machine_id
from gobby.utils.session_context import session_context_for_test
from tests.fixtures.agent_definitions import make_agent_definition
from tests.workflows.test_spawn_scope_rules import _define_agent, _Teams
from tests.workflows.test_spawn_scope_rules import teams as teams_fixture

pytestmark = [pytest.mark.unit, pytest.mark.usefixtures("stub_srt_verifier")]

teams = teams_fixture

TASK_ID = "task-uuid-25125"
SEAT_TYPE = "developer"


def _manager_run(h: _Teams) -> MagicMock:
    """The Dev Manager's own running run, bound to the task it coordinates."""
    assert h.manager.agent_run_id is not None
    run = LocalAgentRunManager(h.db).get(h.manager.agent_run_id)
    assert run is not None
    manager = MagicMock()
    manager.id = run.id
    manager.child_session_id = run.child_session_id
    manager.parent_session_id = run.parent_session_id
    manager.agent_name = run.agent_name
    manager.status = "running"
    manager.task_id = TASK_ID
    return manager


def _worker_run() -> MagicMock:
    """A second worker run bound to the same task."""
    worker = MagicMock()
    worker.id = "run-worker"
    worker.child_session_id = "child-worker"
    worker.parent_session_id = "parent-worker"
    worker.agent_name = "test-writer"
    worker.status = "running"
    worker.task_id = TASK_ID
    return worker


def _run_storage(*active: Any) -> MagicMock:
    storage = MagicMock()
    storage.has_active_run_for_task.return_value = bool(active)
    storage.list_active_global.return_value = list(active)
    storage.get_active_run_for_task.return_value = active[0] if active else None
    storage.get.return_value = None
    storage.get_by_session.return_value = None
    storage.list_by_session.return_value = []
    return storage


def _expire_seat(h: _Teams) -> None:
    """Hold the team's developer seat as an expired, natively resumable session."""
    seat = h.seats[SEAT_TYPE]
    h.db.execute(
        "UPDATE sessions SET source = 'claude', external_id = %s WHERE id = %s",
        ("native-seat-thread", seat.id),
    )
    assert seat.agent_run_id is not None
    LocalAgentRunManager(h.db).cancel(seat.agent_run_id, terminal_reason="daemon_stop")
    SessionManager(h.db).update(seat.id, status="expired")


def _task_manager() -> MagicMock:
    task = MagicMock()
    task.title = "Team task"
    task.seq_num = 25125
    task.id = TASK_ID
    task.category = "code"
    task.additional_skills = None
    task.claimed_by_session_id = None
    task.closed_at = None
    task.stages = []
    tasks = MagicMock()
    tasks.get_task.return_value = task
    return tasks


async def _spawn(
    h: _Teams,
    *,
    caller: Session,
    agent: str,
    resume_session_id: str | None,
    active: tuple[Any, ...],
) -> tuple[dict[str, Any], AsyncMock]:
    """Run spawn_agent through the registry as ``caller``; return the reply and the launch."""
    _define_agent(h.db, "dev-manager", None, variables={"team_manager": True})
    if resume_session_id is not None:
        _expire_seat(h)
    runner = MagicMock()
    runner.can_spawn.return_value = (True, "Can spawn", 0)
    runner.run_storage = _run_storage(*active)
    registry = create_spawn_agent_registry(
        runner,
        task_manager=_task_manager(),
        session_manager=SessionManager(h.db),
        db=h.db,
    )
    context = {"id": caller.project_id, "project_path": "/workspace"}
    body = make_agent_definition(name=agent, prompts={"agent": "Continue"}, model="claude-sonnet-5")
    seat = h.seats[SEAT_TYPE]
    prepared = PreparedSpawn(
        session_id=seat.id,
        agent_run_id="new-run",
        parent_session_id=caller.id,
        project_id=caller.project_id,
        workflow_name=None,
        agent_depth=1,
        env_vars={},
    )
    lease = MagicMock()
    lease.acquire.return_value = None
    lease.attach.return_value = None
    lease.release_unattached.return_value = None
    args: dict[str, Any] = {
        "agent": agent,
        "prompt": "Continue",
        "task_id": "#25125",
        "parent_session_id": caller.id,
    }
    if resume_session_id is not None:
        args["resume_session_id"] = resume_session_id
    with (
        patch.object(_spawn_guards, "get_request_principal", AsyncMock(return_value=None)),
        patch.object(_spawn_guards, "resolve_task_id_for_mcp", return_value=TASK_ID),
        patch.object(_factory, "spawn_agent_impl", _implementation.spawn_agent_impl),
        patch.object(_factory, "_load_agent_body", return_value=body),
        patch.object(_factory, "get_project_context", return_value=context),
        patch.object(_implementation, "get_project_context", return_value=context),
        patch.object(_implementation, "get_isolation_handler", return_value=NoneIsolationHandler()),
        patch.object(_implementation, "TaskSpawnLease", return_value=lease),
        patch("gobby.agents.spawn.prepare_terminal_resume", return_value=prepared),
        patch.object(_implementation, "execute_spawn", AsyncMock()) as execute,
        patch.object(_implementation, "finalize_executed_spawn", AsyncMock()),
        patch.object(_implementation, "persist_initial_step_instance_if_resolved"),
        session_context_for_test(caller.id),
    ):
        result = await registry.call("spawn_agent", args)
        await asyncio.gather(*_implementation._spawn_background_tasks.values())
    return result, execute


@pytest.mark.asyncio
async def test_manager_resumes_its_team_seat_with_task_id_while_its_own_run_is_active(
    teams: _Teams,
) -> None:
    result, execute = await _spawn(
        teams,
        caller=teams.manager,
        agent=SEAT_TYPE,
        resume_session_id=teams.seats[SEAT_TYPE].id,
        active=(_manager_run(teams),),
    )

    assert result.get("skipped") is not True, result
    assert "placement_error" not in result, result
    assert result["success"] is True, result
    execute.assert_awaited_once()


@pytest.mark.asyncio
async def test_manager_resume_is_still_refused_when_a_worker_run_for_the_task_is_active(
    teams: _Teams,
) -> None:
    result, execute = await _spawn(
        teams,
        caller=teams.manager,
        agent=SEAT_TYPE,
        resume_session_id=teams.seats[SEAT_TYPE].id,
        active=(_manager_run(teams), _worker_run()),
    )

    assert result.get("skipped") is True, result
    assert result.get("run_id") == "run-worker", result
    execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_fresh_worker_spawn_is_still_refused_while_a_worker_run_for_the_task_is_active(
    teams: _Teams,
) -> None:
    root = SessionManager(teams.db).register(
        external_id="guard-root-session",
        machine_id=require_machine_id(),
        source="claude",
        project_id=teams.manager.project_id,
    )
    result, execute = await _spawn(
        teams,
        caller=root,
        agent=SEAT_TYPE,
        resume_session_id=None,
        active=(_worker_run(),),
    )

    assert result.get("skipped") is True, result
    assert result.get("run_id") == "run-worker", result
    execute.assert_not_awaited()
