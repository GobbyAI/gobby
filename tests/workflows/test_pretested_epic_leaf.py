"""An epic leaf's approved tests survive claim intake; waits preserve manager contact."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from gobby.hooks.events import HookEventType
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.tasks import LocalTaskManager, Task
from gobby.utils.session_context import session_context_for_test
from gobby.workflows.sync_rules import get_bundled_rules_path, sync_bundled_rules
from tests.fixtures.isolated_checkout import patch_local_machine_id
from tests.fixtures.landing_case import bind_task_manager
from tests.fixtures.review_approvals import grant_step_writes
from tests.workflows.test_seat_step_loops import (
    AUTHORITY_MACHINE_ID,
    COORDINATOR_ID,
    OWNER_ID,
    PEER_ID,
    PROJECT_ID,
    SESSION_ID,
    SeatRun,
)
from tests.workflows.test_test_step_prerequisite import _refresh, _team, _test_record

pytestmark = pytest.mark.unit


@pytest.fixture
def epic_developer(temp_db: HubDatabase, monkeypatch: pytest.MonkeyPatch) -> SeatRun:
    patch_local_machine_id(monkeypatch, AUTHORITY_MACHINE_ID)
    root = Path(__file__).resolve().parents[2]
    definition = yaml.safe_load(
        (root / "src/gobby/install/shared/workflows/agents/developer.yaml").read_text()
    )
    run = SeatRun(temp_db, "developer", definition=definition)
    grant_step_writes(temp_db, PEER_ID, PROJECT_ID, "fixture-test-author", ("test",))
    tasks = LocalTaskManager(temp_db)
    epic = tasks.create_task(PROJECT_ID, "Persistent team epic", task_type="epic")
    _team(run, epic, mode="epic")
    bind_task_manager(
        temp_db, manager=OWNER_ID, parent=COORDINATOR_ID, project_id=PROJECT_ID, task=epic
    )
    return run


def _leaf(run: SeatRun) -> Task:
    tasks = LocalTaskManager(run.db)
    epic = tasks.list_tasks(project_id=PROJECT_ID, task_type="epic")[0]
    return tasks.create_task(
        PROJECT_ID,
        "Pre-tested epic leaf",
        parent_task_id=epic.id,
        category="code",
        validation_criteria="Use the linked candidate's approved test step.",
    )


async def _claim_and_route(run: SeatRun, leaf: Task) -> None:
    await run.load(*run.vars["required_skills"])
    assert run.step == "claim"
    tasks = LocalTaskManager(run.db)
    with session_context_for_test(SESSION_ID):
        tasks.claim_task(leaf.id, SESSION_ID)
    await run.call(
        "gobby-tasks",
        "claim_task",
        {"task_id": leaf.id},
        {"result": {"task_id": leaf.id, "ref": f"#{leaf.seq_num}"}},
    )
    await _refresh(run, leaf)
    if run.step == "load_additional_skills":
        await run.load(*run.vars["additional_skills"])


async def test_claim_of_pretested_epic_leaf_reaches_implementation(epic_developer: SeatRun) -> None:
    leaf = _leaf(epic_developer)
    _test_record(epic_developer, leaf, committed=True)

    await _claim_and_route(epic_developer, leaf)

    assert epic_developer.vars["assigned_task_id"] == leaf.id
    assert epic_developer.step == "implement"


@pytest.mark.parametrize(
    ("destination", "allowed"),
    [
        ({"target": "session", "target_id": OWNER_ID}, True),
        ({"target": "session", "target_id": PEER_ID}, False),
        ({"target": "session", "target_id": COORDINATOR_ID}, False),
        ({"target": "project"}, False),
        ({"target": "global"}, False),
        ({"target": "parent"}, False),
    ],
    ids=["team-manager", "peer", "other-coordinator", "project", "global", "parent"],
)
async def test_await_tests_allows_only_the_team_manager(
    epic_developer: SeatRun, destination: dict[str, Any], allowed: bool
) -> None:
    leaf = _leaf(epic_developer)
    await _claim_and_route(epic_developer, leaf)
    assert epic_developer.step == "await_tests"
    sync_bundled_rules(epic_developer.db, get_bundled_rules_path())
    event = epic_developer._event(
        HookEventType.BEFORE_TOOL,
        {
            "tool_name": "mcp__gobby__call_tool",
            "tool_input": {
                "server_name": "gobby-agents",
                "tool_name": "send_message",
                "arguments": {**destination, "content": "Blocked waiting for test evidence."},
            },
        },
    )
    event.project_id = PROJECT_ID
    event.metadata["_platform_session_id"] = SESSION_ID

    response = await epic_developer.engine.evaluate(
        event, session_id=SESSION_ID, variables=epic_developer.session_vars
    )

    assert (response.decision == "allow") is allowed, response.reason
    assert epic_developer.step == "await_tests"
