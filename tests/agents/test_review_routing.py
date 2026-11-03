"""Team admission binds enabled definitions without interpreting task standards."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from gobby.storage.definitions.agents import AgentDefinitionManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.session_models import Session
from gobby.storage.tasks import LocalTaskManager, Task
from gobby.utils.session_context import session_context_for_test
from tests.fixtures.landing_case import make_landing_case
from tests.fixtures.team_case import TITLE, TeamWorld, make_team_world

pytestmark = pytest.mark.unit


@pytest.fixture
def world(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> TeamWorld:
    world = make_team_world(
        make_landing_case(temp_db, sample_project, tmp_path), tmp_path, monkeypatch
    )
    for name, grants in (
        ("fixture-definition-e08ba693", ["independent-review"]),
        ("fixture-definition-3d77e16a", ["independent-review"]),
        ("fixture-definition-82cde3b3", ["independent-review"]),
        ("fixture-worker", ["implementation"]),
    ):
        AgentDefinitionManager(temp_db).create(
            name,
            {
                "name": name,
                "version": "1.0",
                "prompts": {"agent": f"Run the {name} seat."},
                "workflows": {"rule_selectors": {"include": []}},
                "task_attestation_writes": grants,
            },
            project_id=world.case.project_id,
        )
    return world


async def admit(
    world: TeamWorld, caller: Session, task: Task, reviewer: str, **extra: Any
) -> dict[str, Any]:
    """Call the runbook's admit_team tool as ``caller`` with a reviewer_agent input."""
    arguments = {"task": f"#{task.seq_num}", "reviewer_agent": reviewer, **extra}
    with session_context_for_test(caller.id):
        reply: dict[str, Any] = await world.tools.call("admit_team", arguments)
    return reply


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "definition", ["fixture-worker", "fixture-definition-3d77e16a", "fixture-definition-82cde3b3"]
)
async def test_enabled_definition_needs_no_hardcoded_review_grant(
    world: TeamWorld, definition: str
) -> None:
    task = world.case.task(TITLE)
    owner = world.child(task)

    reply = await admit(world, owner, task, definition)

    assert reply["success"] is True, reply
    assert world.admit_step_output(owner)["team"]["reviewers"] == {definition: None}


async def test_reviewer_agent_admission(world: TeamWorld) -> None:
    task = world.case.task(TITLE)

    # Missing definitions are refused before anything is recorded.
    refused = world.child(task)
    reply = await admit(world, refused, task, "missing-reviewer")
    assert reply["success"] is False, reply
    assert "missing-reviewer" in reply["error"]
    assert world.admit_step_output(refused) == {}

    # A review-grant definition becomes the team's first reviewer binding, with no seat yet.
    owner = world.child(task)
    reply = await admit(world, owner, task, "fixture-definition-3d77e16a")
    assert reply["success"] is True, reply
    team = world.admit_step_output(owner)["team"]
    assert team["reviewers"] == {"fixture-definition-3d77e16a": None}

    # A single-task team keeps exactly one reviewer definition.
    second = await admit(world, owner, task, "fixture-definition-e08ba693")
    assert second["success"] is False, second
    assert "fixture-definition-e08ba693" in second["error"]
    assert world.admit_step_output(owner)["team"]["reviewers"] == {
        "fixture-definition-3d77e16a": None
    }

    # A replacement execution joins with its binding's definition, and no other.
    world.finish(owner)
    mismatch = world.child(task)
    reply = await admit(
        world, mismatch, task, "fixture-definition-e08ba693", team_id=team["team_id"]
    )
    assert reply["success"] is False, reply
    assert "fixture-definition-3d77e16a" in reply["error"]
    replacement = world.child(task)
    reply = await admit(
        world, replacement, task, "fixture-definition-3d77e16a", team_id=team["team_id"]
    )
    assert reply["success"] is True, reply
    assert world.admit_step_output(replacement)["team"]["reviewers"] == {
        "fixture-definition-3d77e16a": None
    }


@pytest.mark.asyncio
async def test_research_spike_admits_a_research_reviewer_devteam(world: TeamWorld) -> None:
    # The owner claims its spike; the bound team takes the declared reviewer definition.
    spike = LocalTaskManager(world.case.db).create_task(
        world.case.project_id,
        "Research the debt",
        task_type="research_spike",
        category="research",
        validation_criteria="The committed report answers the question.",
        claimed_by_session_id=world.case.claimant,
    )
    owner = world.child(spike)
    reply = await admit(world, owner, spike, "fixture-definition-82cde3b3")
    assert reply["success"] is True, reply
    assert world.admit_step_output(owner)["team"]["reviewers"] == {
        "fixture-definition-82cde3b3": None
    }
