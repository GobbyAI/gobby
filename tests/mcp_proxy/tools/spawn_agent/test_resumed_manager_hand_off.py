"""A Dev Manager resumed without task_id still hands off its task's claim (#24976)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from gobby.agents.isolation import NoneIsolationHandler
from gobby.agents.session import ChildSessionManager
from gobby.mcp_proxy.tools.spawn_agent import _implementation as implementation
from gobby.mcp_proxy.tools.spawn_agent._implementation import spawn_agent_impl
from gobby.mcp_proxy.tools.tasks import create_task_registry
from gobby.storage.agents import LocalAgentRunManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.sessions import SessionManager
from gobby.storage.tasks import LocalTaskManager
from gobby.utils.session_context import session_context_for_test
from gobby.workflows.agent_models import (
    AgentDefinitionBody,
    AgentPromptBlocks,
    AgentSelector,
    AgentWorkflows,
)
from tests.fixtures.landing_case import DEV_MANAGER, bind_task_manager, make_landing_case
from tests.fixtures.team_case import make_team_world

pytestmark = [pytest.mark.unit, pytest.mark.usefixtures("stub_srt_verifier")]


def _seat(db: HubDatabase, session_id: str, runbook: str, role: str) -> None:
    LocalAgentRunManager(db).create(
        parent_session_id=runbook,
        provider="claude",
        prompt=f"Run the {role} seat.",
        agent_name=role,
        child_session_id=session_id,
    )


def _park(db: HubDatabase, session_id: str) -> str:
    """End the seat's run the way a crash does, leaving a resumable session."""
    runs = LocalAgentRunManager(db)
    run = runs.get_by_session(session_id)
    assert run is not None and run.task_id is not None
    runs.start(run.id)
    assert runs.cancel(run.id, terminal_reason="daemon_stop") is not None
    db.execute(
        "UPDATE sessions SET source = 'claude', status = 'expired', agent_run_id = %s "
        "WHERE id = %s",
        (run.id, session_id),
    )
    return run.id


@pytest.mark.asyncio
async def test_resumed_dev_manager_hands_off_its_tasks_claim(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = make_landing_case(temp_db, sample_project, tmp_path)
    world = make_team_world(case, tmp_path, monkeypatch)
    tasks = LocalTaskManager(temp_db)
    task = case.task("Resumed manager hand-off")
    runbook = world.child(task)
    await world.admit(runbook, task)
    manager = case.session("dev-manager", parent=runbook.id)
    bind_task_manager(
        temp_db, manager=manager, parent=runbook.id, project_id=case.project_id, task=task
    )
    writer = case.session("test-writer", parent=runbook.id)
    developer = case.session("developer", parent=runbook.id)
    _seat(temp_db, writer, runbook.id, "test-writer")
    _seat(temp_db, developer, runbook.id, "developer")
    tasks.claim_task(task.id, writer, force=True)
    previous_run = _park(temp_db, manager)

    runner = MagicMock()
    runner.can_spawn.return_value = (True, "Can spawn", 0)
    runner.run_storage = LocalAgentRunManager(temp_db)
    runner.child_session_manager = ChildSessionManager(SessionManager(temp_db))
    context = {"id": case.project_id, "project_path": str(case.repo)}
    with (
        patch.object(implementation, "get_project_context", return_value=context),
        patch.object(implementation, "get_isolation_handler", return_value=NoneIsolationHandler()),
        patch.object(implementation, "execute_spawn", AsyncMock()),
        patch.object(implementation, "finalize_executed_spawn", AsyncMock()),
        patch.object(implementation, "persist_initial_step_instance_if_resolved"),
    ):
        resumed = await spawn_agent_impl(
            prompt="Continue managing the task",
            runner=runner,
            task_manager=tasks,
            session_manager=SessionManager(temp_db),
            parent_session_id=runbook.id,
            target_project_id=case.project_id,
            project_path=str(case.repo),
            resume_session_id=manager,
            reserved_run_id=str(uuid4()),
            agent_body=AgentDefinitionBody(
                name=DEV_MANAGER,
                prompts=AgentPromptBlocks(agent="Manage the task"),
                workflows=AgentWorkflows(rule_selectors=AgentSelector()),
            ),
            db=temp_db,
        )
        await asyncio.gather(*implementation._spawn_background_tasks.values())
    assert resumed["success"] is True, resumed
    successor = LocalAgentRunManager(temp_db).get_by_session(manager)
    assert successor is not None and successor.id != previous_run
    assert successor.task_id == task.id

    with session_context_for_test(manager):
        handed = cast(
            dict[str, Any],
            await create_task_registry(tasks).call(
                "hand_off_task_claim",
                {
                    "task_id": task.id,
                    "holder_session_ref": writer,
                    "recipient_session_ref": developer,
                    "reason": "Tests are committed; implementation starts",
                },
            ),
        )
    assert handed.get("error_code") != "team_manager_required", handed
    assert handed.get("success") is True, handed
    assert tasks.get_task(task.id).claimed_by_session_id == developer
