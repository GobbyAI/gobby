"""The shipped plan-writer seat records the implementation step for its stamped plan.

A planning task closes only with approved implementation and closing records. The Plan
Writer records the implementation step for the plan commit it stamps. Closing stays with
a coordinator, so the Plan Writer's closing record is refused.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from gobby.agents.sync import sync_bundled_agents
from gobby.mcp_proxy.tools.tasks import create_task_registry
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.sessions import SessionManager
from gobby.storage.tasks import LocalTaskManager
from gobby.tasks.step_records import task_step_requirements
from gobby.utils.machine_id import require_machine_id
from gobby.utils.session_context import session_context_for_test
from gobby.workflows.agent_resolver import session_agent_definition
from gobby.workflows.state_manager import SessionVariableManager
from tests.fixtures.agent_definitions import seed_seat
from tests.tasks.test_close_invariants import _set_close_policy, git

pytestmark = pytest.mark.unit


def _register_seat(db: HubDatabase, project_id: str, external_id: str, agent_type: str) -> str:
    session = SessionManager(db).register(
        external_id=external_id,
        machine_id=require_machine_id(),
        source="claude",
        project_id=project_id,
    )
    SessionVariableManager(db).set_variable(session.id, "_agent_type", agent_type)
    return session.id


def test_shipped_plan_writer_grants_only_the_implementation_step(
    temp_db: HubDatabase, sample_project: dict[str, Any]
) -> None:
    sync_bundled_agents(temp_db)
    session_id = _register_seat(temp_db, sample_project["id"], "pw-grant", "plan-writer")
    session = SessionManager(temp_db).get(session_id)
    assert session is not None

    definition = session_agent_definition(temp_db, session)

    assert definition is not None
    body, _digest = definition
    assert body.name == "plan-writer"
    assert body.task_step_writes == ["implementation"]


@pytest.mark.asyncio
async def test_plan_writer_records_implementation_for_its_stamped_plan(
    temp_db: HubDatabase, sample_git_project: dict[str, Any]
) -> None:
    project_id = sample_git_project["id"]
    repo = Path(sample_git_project["repo_path"])
    _set_close_policy(temp_db, project_id, {"enforce_step_records": True})
    sync_bundled_agents(temp_db)
    seed_seat(temp_db, "coordinator", task_step_writes=["closing"])

    manager = LocalTaskManager(temp_db)
    plan_writer = _register_seat(temp_db, project_id, "pw-stamp", "plan-writer")
    coordinator = _register_seat(temp_db, project_id, "coord-close", "coordinator")
    task = manager.create_task(
        project_id,
        "Plan the migration",
        category="planning",
        validation_criteria="The plan is stamped and consensus is recorded.",
    )
    manager.claim_task(task.id, plan_writer)

    plan = repo / ".gobby/plans/plan-migration.md"
    plan.parent.mkdir(parents=True, exist_ok=True)
    plan.write_text("# Plan migration\n\n## V1 Plan Changelog\n\n- Consensus reached.\n")
    git(repo, "add", str(plan))
    git(repo, "commit", "-m", "stamped plan")
    sha = git(repo, "rev-parse", "HEAD")

    registry = create_task_registry(manager)
    with session_context_for_test(plan_writer):
        linked = await registry.call("link_commit", {"task_id": task.id, "commit_sha": sha})
        assert "error" not in linked, linked
        task = manager.get_task(task.id)
        assert task_step_requirements(temp_db, task).missing == ("implementation", "closing")

        recorded = await registry.call(
            "record_implementation_step",
            {
                "task_id": task.id,
                "status": "approved",
                "epoch": task.review_epoch,
                "round_number": 1,
                "feedback": "The stamped plan commit is the implementation.",
                "commit_sha": sha,
            },
        )
        assert "error" not in recorded, recorded

        refused = await registry.call(
            "record_closing_step",
            {
                "task_id": task.id,
                "status": "approved",
                "epoch": task.review_epoch,
                "round_number": 1,
                "feedback": "A plan writer closing its own plan.",
                "commit_sha": sha,
            },
        )
        assert refused == {"error": "agent definition plan-writer cannot write the closing step"}

    task = manager.get_task(task.id)
    assert task_step_requirements(temp_db, task).missing == ("closing",)

    with session_context_for_test(coordinator):
        closing = await registry.call(
            "record_closing_step",
            {
                "task_id": task.id,
                "status": "approved",
                "epoch": task.review_epoch,
                "round_number": 1,
                "feedback": "The coordinator's closing step: the plan is stamped.",
                "commit_sha": sha,
            },
        )
    assert "error" not in closing, closing
    assert task_step_requirements(temp_db, manager.get_task(task.id)).missing == ()
