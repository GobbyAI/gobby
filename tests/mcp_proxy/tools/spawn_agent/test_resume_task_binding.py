"""A resumed seat keeps the task binding its previous run held (#24976)."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.agents.isolation import NoneIsolationHandler
from gobby.agents.spawn import PreparedSpawn
from gobby.mcp_proxy.tools.spawn_agent import _implementation as implementation
from gobby.mcp_proxy.tools.spawn_agent import _resume as resume
from gobby.mcp_proxy.tools.spawn_agent import _spawn_guards as spawn_guards
from gobby.mcp_proxy.tools.spawn_agent._implementation import spawn_agent_impl
from gobby.tasks.state_semantics import is_task_actionable
from tests.fixtures.agent_definitions import make_agent_definition

pytestmark = [pytest.mark.unit, pytest.mark.usefixtures("stub_srt_verifier")]


def _task(task_id: str, seq_num: int, closed_at: str | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        id=task_id,
        title=f"Task {seq_num}",
        seq_num=seq_num,
        status="closed" if closed_at else "in_progress",
        closed_at=closed_at,
        escalated_at=None,
        additional_skills=None,
        claimed_by_session_id=None,
    )


TASKS = {
    task.id: task
    for task in (
        _task("task-open", 101),
        _task("task-other", 102),
        _task("task-closed", 103, closed_at="2026-10-25T06:00:00+00:00"),
    )
}


@pytest.fixture(autouse=True)
def stored_agent_type() -> Iterator[None]:
    """Every resume target is a stored 'dev-manager' seat."""
    with patch.object(resume, "SessionVariableManager") as manager:
        manager.return_value.get_variable_subset.return_value = {"_agent_type": "dev-manager"}
        yield


def _previous_run(task_id: str) -> SimpleNamespace:
    """The seat's last run: ended, bound to ``task_id``, in no isolated checkout."""
    return SimpleNamespace(
        id="old-run",
        status="cancelled",
        agent_name="dev-manager",
        task_id=task_id,
        child_session_id="existing",
        parent_session_id="old-parent",
        worktree_id=None,
        clone_id=None,
        resume_metadata_json={},
    )


async def _resume_seat(previous_task: str, explicit_task: str | None) -> tuple[dict[str, Any], Any]:
    """Resume the seat through spawn_agent_impl; return the reply and the resume preparation."""
    target = SimpleNamespace(
        id="existing",
        project_id="project",
        source="claude",
        external_id="native-thread",
        status="expired",
        agent_run_id="old-run",
        parent_session_id="old-parent",
        workflow_name=None,
        workspace_path=None,
        model=None,
        reasoning_effort=None,
    )
    sessions = MagicMock()
    sessions.resolve_session_reference.return_value = target.id
    sessions.get.return_value = target
    runner = MagicMock()
    runner.can_spawn.return_value = (True, "Can spawn", 0)
    runner.run_storage.has_active_run_for_task.return_value = False
    previous = _previous_run(previous_task)
    runner.run_storage.get.side_effect = lambda run_id: previous if run_id == "old-run" else None
    runner.run_storage.get_by_session.return_value = None
    runner.run_storage.list_by_session.return_value = [previous]
    tasks = MagicMock()
    tasks.get_task.side_effect = TASKS.get
    prepared = PreparedSpawn(
        session_id="existing",
        agent_run_id="new-run",
        parent_session_id="parent",
        project_id="project",
        workflow_name=None,
        agent_depth=1,
        env_vars={},
    )
    # A task-bound spawn holds the task's dispatch mutex; the mocked hub cannot.
    lease = MagicMock()
    lease.acquire.return_value = None
    lease.attach.return_value = None
    with (
        patch.object(implementation, "TaskSpawnLease", return_value=lease),
        patch.object(
            implementation,
            "get_project_context",
            return_value={"id": "project", "project_path": "/workspace"},
        ),
        patch.object(implementation, "get_machine_id", return_value="machine"),
        patch.object(implementation, "get_isolation_handler", return_value=NoneIsolationHandler()),
        patch.object(spawn_guards, "resolve_task_id_for_mcp", side_effect=lambda _m, ref, _p: ref),
        patch("gobby.agents.spawn.prepare_terminal_resume", return_value=prepared) as prepare,
        patch.object(implementation, "execute_spawn", AsyncMock()),
        patch.object(implementation, "finalize_executed_spawn", AsyncMock()),
        patch.object(implementation, "persist_initial_step_instance_if_resolved"),
    ):
        result = await spawn_agent_impl(
            prompt="Continue",
            runner=runner,
            task_id=explicit_task,
            task_manager=tasks,
            session_manager=sessions,
            parent_session_id="parent",
            target_project_id="project",
            resume_session_id="gobby#123",
            reserved_run_id="new-run",
            agent_body=make_agent_definition(name="dev-manager", prompts={"agent": "Continue"}),
            db=MagicMock(),
        )
        await asyncio.gather(*implementation._spawn_background_tasks.values())
    return result, prepare


@pytest.mark.asyncio
async def test_resume_without_task_id_inherits_the_previous_runs_open_task() -> None:
    assert is_task_actionable(TASKS["task-open"])
    result, prepare = await _resume_seat("task-open", explicit_task=None)

    assert result["success"] is True, result
    assert prepare.call_args.kwargs["task_id"] == "task-open"


@pytest.mark.asyncio
async def test_explicit_task_id_on_resume_wins_over_the_previous_binding() -> None:
    result, prepare = await _resume_seat("task-open", explicit_task="task-other")

    assert result["success"] is True, result
    assert prepare.call_args.kwargs["task_id"] == "task-other"


@pytest.mark.asyncio
async def test_resume_does_not_inherit_a_closed_tasks_binding() -> None:
    assert not is_task_actionable(TASKS["task-closed"])
    result, prepare = await _resume_seat("task-closed", explicit_task=None)

    # The seat still resumes; it comes back unbound rather than refused.
    assert result["success"] is True, result
    assert prepare.call_args.kwargs["task_id"] is None
