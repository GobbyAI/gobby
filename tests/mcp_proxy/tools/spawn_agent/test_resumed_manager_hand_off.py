"""Team seats resumed from another session keep their team and task binding (#24976)."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from gobby.agents.isolation import NoneIsolationHandler
from gobby.agents.session import ChildSessionManager
from gobby.agents.team_lifecycle import TeamBinding, seat_team
from gobby.mcp_proxy.tools.spawn_agent import _implementation as implementation
from gobby.mcp_proxy.tools.spawn_agent._implementation import spawn_agent_impl
from gobby.mcp_proxy.tools.tasks import create_task_registry
from gobby.storage.agents import LocalAgentRunManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.sessions import SessionManager
from gobby.storage.tasks import LocalTaskManager, Task
from gobby.utils.session_context import session_context_for_test
from gobby.workflows.agent_models import (
    AgentDefinitionBody,
    AgentPromptBlocks,
    AgentSelector,
    AgentWorkflows,
)
from tests.fixtures.landing_case import (
    DEV_MANAGER,
    LandingCase,
    bind_task_manager,
    make_landing_case,
)
from tests.fixtures.team_case import make_team_world

pytestmark = [pytest.mark.unit, pytest.mark.usefixtures("stub_srt_verifier")]


@dataclass
class CrashedTeam:
    """A devteam whose seats all crashed; an unrelated session will resume them."""

    case: LandingCase
    tasks: LocalTaskManager
    task: Task
    team: TeamBinding
    resumer: str
    manager: str
    writer: str
    developer: str


def _seat(db: HubDatabase, session_id: str, runbook: str, role: str) -> None:
    LocalAgentRunManager(db).create(
        parent_session_id=runbook,
        provider="claude",
        prompt=f"Run the {role} seat.",
        agent_name=role,
        child_session_id=session_id,
    )


def _park(db: HubDatabase, session_id: str) -> None:
    """End the seat's run the way a crash does, leaving a resumable session."""
    runs = LocalAgentRunManager(db)
    run = runs.get_by_session(session_id)
    assert run is not None
    runs.start(run.id)
    assert runs.cancel(run.id, terminal_reason="daemon_stop") is not None
    db.execute(
        "UPDATE sessions SET source = 'claude', status = 'expired', agent_run_id = %s "
        "WHERE id = %s",
        (run.id, session_id),
    )


@pytest.fixture
async def crashed(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> CrashedTeam:
    case = make_landing_case(temp_db, sample_project, tmp_path)
    world = make_team_world(case, tmp_path, monkeypatch)
    tasks = LocalTaskManager(temp_db)
    task = case.task("Resumed manager hand-off")
    runbook = world.child(task)
    team = await world.admit(runbook, task)
    manager = case.session("dev-manager", parent=runbook.id)
    bind_task_manager(
        temp_db, manager=manager, parent=runbook.id, project_id=case.project_id, task=task
    )
    writer = case.session("test-writer", parent=runbook.id)
    developer = case.session("developer", parent=runbook.id)
    _seat(temp_db, writer, runbook.id, "test-writer")
    _seat(temp_db, developer, runbook.id, "developer")
    tasks.claim_task(task.id, writer, force=True)
    for seat in (manager, developer):
        _park(temp_db, seat)
    # The Orchestrator, not the team runbook, resumes the crashed seats.
    resumer = case.session("orchestrator")
    return CrashedTeam(case, tasks, task, team, resumer, manager, writer, developer)


async def _resume(crashed: CrashedTeam, seat: str, agent_name: str) -> dict[str, Any]:
    """Resume ``seat`` through spawn_agent_impl from the resumer, without a task_id."""
    db = crashed.case.db
    runner = MagicMock()
    runner.can_spawn.return_value = (True, "Can spawn", 0)
    runner.run_storage = LocalAgentRunManager(db)
    runner.child_session_manager = ChildSessionManager(SessionManager(db))
    context = {"id": crashed.case.project_id, "project_path": str(crashed.case.repo)}
    with (
        patch.object(implementation, "get_project_context", return_value=context),
        patch.object(implementation, "get_isolation_handler", return_value=NoneIsolationHandler()),
        patch.object(implementation, "execute_spawn", AsyncMock()),
        patch.object(implementation, "finalize_executed_spawn", AsyncMock()),
        patch.object(implementation, "persist_initial_step_instance_if_resolved"),
    ):
        resumed = await spawn_agent_impl(
            prompt="Continue the seat",
            runner=runner,
            task_manager=crashed.tasks,
            session_manager=SessionManager(db),
            parent_session_id=crashed.resumer,
            caller_session_id=crashed.resumer,
            target_project_id=crashed.case.project_id,
            project_path=str(crashed.case.repo),
            resume_session_id=seat,
            reserved_run_id=str(uuid4()),
            agent_body=AgentDefinitionBody(
                name=agent_name,
                prompts=AgentPromptBlocks(agent="Continue the seat"),
                model="claude-sonnet-5",
                workflows=AgentWorkflows(rule_selectors=AgentSelector()),
            ),
            db=db,
        )
        await asyncio.gather(*implementation._spawn_background_tasks.values())
    return resumed


@pytest.mark.asyncio
async def test_seat_resumed_from_another_session_keeps_its_team(crashed: CrashedTeam) -> None:
    db = crashed.case.db
    assert seat_team(db, crashed.developer) == crashed.team

    resumed = await _resume(crashed, crashed.developer, "developer")

    assert resumed["success"] is True, resumed
    assert seat_team(db, crashed.developer) == crashed.team


@pytest.mark.asyncio
async def test_seat_resumed_past_32_runs_keeps_its_team(crashed: CrashedTeam) -> None:
    """The Orchestrator resumes a seat 33 times; the runbook run is 33 links back on its chain."""
    db = crashed.case.db
    for _ in range(33):
        resumed = await _resume(crashed, crashed.developer, "developer")
        assert resumed["success"] is True, resumed
        _park(db, crashed.developer)

    seat = SessionManager(db).get(crashed.developer)
    assert seat is not None
    assert seat.parent_session_id == crashed.resumer
    assert seat_team(db, crashed.developer) == crashed.team


@pytest.mark.asyncio
async def test_resumed_dev_manager_hands_off_to_a_resumed_seat(crashed: CrashedTeam) -> None:
    db = crashed.case.db
    for seat, agent_name in ((crashed.manager, DEV_MANAGER), (crashed.developer, "developer")):
        resumed = await _resume(crashed, seat, agent_name)
        assert resumed["success"] is True, resumed
    successor = LocalAgentRunManager(db).get_by_session(crashed.manager)
    assert successor is not None
    assert successor.task_id == crashed.task.id

    with session_context_for_test(crashed.manager):
        handed = cast(
            dict[str, Any],
            await create_task_registry(crashed.tasks).call(
                "hand_off_task_claim",
                {
                    "task_id": crashed.task.id,
                    "holder_session_ref": crashed.writer,
                    "recipient_session_ref": crashed.developer,
                    "reason": "Tests are committed; implementation starts",
                },
            ),
        )
    assert handed.get("success") is True, handed
    assert crashed.tasks.get_task(crashed.task.id).claimed_by_session_id == crashed.developer
