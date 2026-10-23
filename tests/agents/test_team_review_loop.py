"""The review loop passes one task claim between the seats of one team (plan 3.2)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, cast

import pytest

from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.mcp_proxy.tools.tasks import create_task_registry
from gobby.storage.agents import LocalAgentRunManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.session_models import Session
from gobby.storage.tasks import LocalTaskManager, Task
from gobby.storage.tasks._lifecycle_events import TaskLifecycleEventManager
from gobby.utils.session_context import session_context_for_test
from gobby.workflows.state_manager import SessionVariableManager
from tests.fixtures.landing_case import bind_task_manager, make_landing_case
from tests.fixtures.team_case import TITLE, TeamWorld, git_in, make_team_world

pytestmark = pytest.mark.unit


@pytest.fixture
def world(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> TeamWorld:
    return make_team_world(
        make_landing_case(temp_db, sample_project, tmp_path), tmp_path, monkeypatch
    )


def seat(world: TeamWorld, runbook: Session, role: str, *, manages: Task | None = None) -> str:
    """A seat the team runbook launched; ``manages`` binds the Dev Manager's run to its task."""
    session = world.case.session(role, parent=runbook.id)
    if manages is not None:
        bind_task_manager(
            world.case.db,
            manager=session,
            parent=runbook.id,
            project_id=world.case.project_id,
            task=manages,
        )
    else:
        LocalAgentRunManager(world.case.db).create(
            parent_session_id=runbook.id,
            provider="claude",
            prompt=f"Run the {role} seat.",
            agent_name=role,
            child_session_id=session,
        )
    return session


async def hand_off(
    tools: InternalToolRegistry, caller: str, task: Task, holder: str, recipient: str
) -> dict[str, Any]:
    arguments = {
        "task_id": task.id,
        "holder_session_ref": holder,
        "recipient_session_ref": recipient,
        "reason": "Tests are committed; implementation starts",
    }
    with session_context_for_test(caller):
        return cast(dict[str, Any], await tools.call("hand_off_task_claim", arguments))


def claimed_tasks(db: HubDatabase, session_id: str) -> dict[str, Any]:
    claimed: dict[str, Any] = (
        SessionVariableManager(db).get_variables(session_id).get("claimed_tasks") or {}
    )
    return claimed


@pytest.mark.asyncio
async def test_claim_handoff_is_scoped_and_atomic(world: TeamWorld) -> None:
    db = world.case.db
    tasks = LocalTaskManager(db)
    tools = create_task_registry(tasks)
    task = world.case.task(TITLE)
    runbook = world.child(task)
    team = await world.admit(runbook, task)
    manager = seat(world, runbook, "dev-manager", manages=task)
    writer, developer, reviewer = (
        seat(world, runbook, role) for role in ("test-writer", "developer", "code-reviewer")
    )
    other_task = world.case.task("Another team's task")
    other_runbook = world.child(other_task)
    await world.admit(other_runbook, other_task)
    other_manager = seat(world, other_runbook, "dev-manager", manages=other_task)
    outsider = seat(world, other_runbook, "developer")
    tasks.claim_task(task.id, writer, force=True)

    def holder() -> str | None:
        return tasks.get_task(task.id).claimed_by_session_id

    # Only this team's Dev Manager moves the claim, and only between this team's seats.
    refused = await hand_off(tools, other_manager, task, writer, developer)
    assert refused.get("error_code") == "team_manager_required", refused
    refused = await hand_off(tools, developer, task, writer, developer)
    assert refused.get("error_code") == "team_manager_required", refused
    refused = await hand_off(tools, manager, task, writer, outsider)
    assert refused.get("error_code") == "team_seat_required", refused
    # A stale expected owner never moves the claim.
    refused = await hand_off(tools, manager, task, developer, reviewer)
    assert refused.get("error_code") == "task_claim_conflict", refused
    assert holder() == writer

    # Uncommitted task-attributed work keeps the claim with its holder.
    tree = Path(team.worktree_path)
    (tree / "test_scroll.py").write_text("def test_scroll() -> None: ...\n")
    SessionVariableManager(db).merge_variables(
        writer,
        {
            "task_edited_files": {task.id: ["test_scroll.py"]},
            "task_edited_file_checkouts": {task.id: {str(tree): ["test_scroll.py"]}},
        },
    )
    refused = await hand_off(tools, manager, task, writer, developer)
    assert refused.get("error_code") == "task_has_uncommitted_changes", refused
    assert holder() == writer
    git_in(tree, "add", "test_scroll.py")
    git_in(tree, "commit", "-m", "tests first")

    # Two concurrent hand-offs from the same holder: exactly one moves the claim.
    results = await asyncio.gather(
        hand_off(tools, manager, task, writer, developer),
        hand_off(tools, manager, task, writer, reviewer),
    )
    winners = [result for result in results if result.get("success") is True]
    assert len(winners) == 1, results
    (loser,) = (result for result in results if result.get("success") is not True)
    assert loser.get("error_code") == "task_claim_conflict", results
    recipient = winners[0]["handed_to_session_id"]
    assert recipient in (developer, reviewer)
    assert holder() == recipient
    assert task.id in claimed_tasks(db, recipient)
    assert task.id not in claimed_tasks(db, writer)
    event = TaskLifecycleEventManager(db).list_lifecycle_events(task.id)[-1]
    assert event.by_actor == manager
    assert writer in event.reason and recipient in event.reason

    # The same hand-off replayed is stale and changes nothing.
    replay = await hand_off(tools, manager, task, writer, recipient)
    assert replay.get("error_code") == "task_claim_conflict", replay
    assert holder() == recipient
