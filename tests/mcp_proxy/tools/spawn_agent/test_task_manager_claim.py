"""Task-bound managers must remain independent of the task's claim history."""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.agents.isolation import IsolationContext
from gobby.mcp_proxy.tools.spawn_agent import create_spawn_agent_registry
from gobby.storage.agents import LocalAgentRunManager
from gobby.storage.definitions.agents import AgentDefinitionManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.session_tasks import claimant_sessions
from gobby.storage.sessions import SessionManager
from gobby.storage.tasks import LocalTaskManager, Task
from gobby.workflows.definitions import AgentDefinitionBody
from tests.fixtures.agent_definitions import make_agent_definition, make_agent_workflows
from tests.fixtures.landing_case import LandingCase, make_landing_case
from tests.fixtures.review_approvals import grant_step_writes
from tests.mcp_proxy.tools.spawn_agent.test_execution import (
    _drain_spawn_background_tasks,
    _spawn_success,
)

pytestmark = [pytest.mark.unit, pytest.mark.usefixtures("stub_srt_verifier")]


@pytest.fixture
def case(temp_db: HubDatabase, sample_project: dict[str, object], tmp_path: Path) -> LandingCase:
    return make_landing_case(temp_db, sample_project, tmp_path)


def _seat_definition(seat: str) -> AgentDefinitionBody:
    """A team manager closes without claiming; a developer implements and auto-claims."""
    if seat == "dev-manager":
        return make_agent_definition(
            name=seat,
            prompts={"agent": "Synthetic team manager."},
            model="claude-sonnet-5",
            capabilities=["manage_task_claims", "land_tasks", "accept_resume_retype"],
            task_attestation_writes=["closing"],
            workflows=make_agent_workflows(variables={"team_manager": True}),
        )
    return make_agent_definition(
        name=seat,
        prompts={"agent": "Synthetic developer."},
        model="claude-sonnet-5",
        task_attestation_writes=["implementation"],
    )


async def _spawn_task_seat(
    case: LandingCase, task: Task, mock_runner: MagicMock, seat: str, *, claim: bool = True
) -> str:
    body = _seat_definition(seat)
    # The spawned session's authority is read from the installed row, not the patched body.
    AgentDefinitionManager(case.db).create(
        name=seat, source="custom", definition_json=body.model_dump(mode="json", exclude_none=True)
    )
    runs = LocalAgentRunManager(case.db)
    mock_runner.run_storage = runs
    registry = create_spawn_agent_registry(
        mock_runner,
        task_manager=LocalTaskManager(case.db),
        db=case.db,
        session_manager=SessionManager(case.db),
    )
    handler = MagicMock()
    handler.prepare_environment = AsyncMock(return_value=IsolationContext(cwd=str(case.repo)))
    handler.build_context_prompt.return_value = "Run the assigned task."
    with (
        patch("gobby.mcp_proxy.tools.spawn_agent._factory._load_agent_body", return_value=body),
        patch(
            "gobby.mcp_proxy.tools.spawn_agent._implementation.get_project_context",
            return_value={"id": case.project_id, "project_path": str(case.repo)},
        ),
        patch(
            "gobby.mcp_proxy.tools.spawn_agent._implementation.get_isolation_handler",
            return_value=handler,
        ),
        patch(
            "gobby.mcp_proxy.tools.spawn_agent._implementation.execute_spawn",
            new=_spawn_success(runs),
        ),
    ):
        result = await registry.call(
            "spawn_agent",
            {
                "agent": seat,
                "provider": "claude",
                "prompt": "Run the assigned task.",
                "parent_session_id": case.creator,
                "task_id": task.id,
                "claim": claim,
                "checkout_mode": "none",
            },
        )
        await _drain_spawn_background_tasks()
    assert result["success"] is True, result
    run = runs.get(result["run_id"])
    assert run is not None
    assert run.task_id == task.id
    assert run.child_session_id is not None
    assert (run.resume_metadata_json or {})["claim"] is claim
    return run.child_session_id


@pytest.mark.asyncio
async def test_explicit_spawn_task_is_claimed_despite_closing_grants(
    case: LandingCase, mock_runner: MagicMock
) -> None:
    tasks = LocalTaskManager(case.db)
    task = tasks.create_task(
        case.project_id, "Manager binding", validation_criteria="Stay independent"
    )
    manager = await _spawn_task_seat(case, task, mock_runner, "dev-manager")

    with case.db.transaction() as conn:
        claimants = claimant_sessions(conn, task.id)
    assert manager in claimants
    assert tasks.get_task(task.id).claimed_by_session_id == manager


@pytest.mark.asyncio
async def test_spawned_task_holder_can_land_its_own_candidate(
    case: LandingCase, mock_runner: MagicMock
) -> None:
    sha = case.candidate("manager-landing", {"docs/change.md": "reviewed\n"})
    tasks = LocalTaskManager(case.db)
    task = tasks.create_task(
        case.project_id, "Manager landing", validation_criteria="Land reviewed SHA"
    )
    manager = await _spawn_task_seat(case, task, mock_runner, "dev-manager")
    case.link(task, sha)
    grant_step_writes(
        case.db,
        manager,
        case.project_id,
        "dev-manager",
        ("closing",),
        capabilities=("land_tasks",),
    )

    landed = await case.land(tasks.get_task(task.id), sha, caller=manager)

    assert "caller_is_maker" not in landed.get("blockers", [])
    assert landed["landed"] is True, landed
    assert case.git("rev-parse", "trunk") == sha


@pytest.mark.asyncio
async def test_spawned_developer_still_auto_claims(
    case: LandingCase, mock_runner: MagicMock
) -> None:
    tasks = LocalTaskManager(case.db)
    task = tasks.create_task(
        case.project_id, "Developer claim", validation_criteria="Own implementation"
    )
    developer = await _spawn_task_seat(case, task, mock_runner, "developer")

    assert tasks.get_task(task.id).claimed_by_session_id == developer
    with case.db.transaction() as conn:
        assert claimant_sessions(conn, task.id) == {developer}


@pytest.mark.asyncio
@pytest.mark.parametrize("release_claim", [False, True], ids=["current", "former"])
async def test_current_or_former_claim_does_not_impose_a_runbook_role(
    case: LandingCase, release_claim: bool
) -> None:
    sha = case.candidate("claimant-landing", {"docs/change.md": "reviewed\n"})
    task = case.task()
    if release_claim:
        LocalTaskManager(case.db).claim_task(task.id, case.delegator, force=True)
    case.link(task, sha)
    case.approve(task, sha)
    manager = case.manager_for(task, session=case.claimant)

    landed = await case.land(LocalTaskManager(case.db).get_task(task.id), sha, caller=manager)

    assert landed["landed"] is True, landed
    assert case.git("rev-parse", "trunk") == sha


@pytest.mark.asyncio
@pytest.mark.parametrize("owned", [False, True])
async def test_assignment_without_claim_preserves_ownership(
    case: LandingCase, mock_runner: MagicMock, owned: bool
) -> None:
    tasks = LocalTaskManager(case.db)
    task = tasks.create_task(
        case.project_id, "Assigned observer", validation_criteria="Preserve ownership"
    )
    if owned:
        tasks.claim_task(task.id, session_id=case.creator)
    child = await _spawn_task_seat(case, task, mock_runner, "code-reviewer", claim=False)
    assert tasks.get_task(task.id).claimed_by_session_id == (case.creator if owned else None)
    with case.db.transaction() as conn:
        assert child not in claimant_sessions(conn, task.id)
