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
        ("code-reviewer", ["independent-review"]),
        ("docs-reviewer", ["independent-review"]),
        ("research-reviewer", ["independent-review"]),
        ("developer", ["implementation"]),
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
@pytest.mark.parametrize("definition", ["developer", "docs-reviewer", "research-reviewer"])
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
    reply = await admit(world, owner, task, "docs-reviewer")
    assert reply["success"] is True, reply
    team = world.admit_step_output(owner)["team"]
    assert team["reviewers"] == {"docs-reviewer": None}

    # A single-task team keeps exactly one reviewer definition.
    second = await admit(world, owner, task, "code-reviewer")
    assert second["success"] is False, second
    assert "code-reviewer" in second["error"]
    assert world.admit_step_output(owner)["team"]["reviewers"] == {"docs-reviewer": None}

    # A replacement execution joins with its binding's definition, and no other.
    world.finish(owner)
    mismatch = world.child(task)
    reply = await admit(world, mismatch, task, "code-reviewer", team_id=team["team_id"])
    assert reply["success"] is False, reply
    assert "docs-reviewer" in reply["error"]
    replacement = world.child(task)
    reply = await admit(world, replacement, task, "docs-reviewer", team_id=team["team_id"])
    assert reply["success"] is True, reply
    assert world.admit_step_output(replacement)["team"]["reviewers"] == {"docs-reviewer": None}


@pytest.mark.asyncio
async def test_research_spike_admits_a_research_reviewer_devteam(world: TeamWorld) -> None:
    # The researcher claims its spike; the DevTeam bound to it takes research-reviewer.
    spike = LocalTaskManager(world.case.db).create_task(
        world.case.project_id,
        "Research the debt",
        task_type="research_spike",
        category="research",
        validation_criteria="The committed report answers the question.",
        claimed_by_session_id=world.case.claimant,
    )
    owner = world.child(spike)
    reply = await admit(world, owner, spike, "research-reviewer")
    assert reply["success"] is True, reply
    assert world.admit_step_output(owner)["team"]["reviewers"] == {"research-reviewer": None}
