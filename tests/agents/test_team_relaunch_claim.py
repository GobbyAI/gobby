"""A relaunched team seat takes over the claim its dead predecessor held (#25719).

Synthetic definitions only: the seat agents and the claim_flow stages are fixture names.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from gobby.agents.team_admission_records import team_binding
from gobby.mcp_proxy.tools.spawn_agent import _factory
from gobby.storage.definitions.agents import AgentDefinitionManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.tasks import LocalTaskManager
from gobby.storage.team_records import ADMISSION_STEP
from gobby.tasks.attestations import AttestationSubmission, record_attestation
from gobby.utils.session_context import session_context_for_test
from gobby.workflows.definitions import PipelineDefinition, PipelineStep
from gobby.workflows.pipeline_executor import step_invocation_id
from gobby.workflows.pipeline_models import ClaimStage
from gobby.workflows.pipeline_state import StepStatus
from tests.fixtures.landing_case import make_landing_case
from tests.fixtures.team_case import (
    TITLE,
    TeamWorld,
    execution_id,
    make_team_world,
    running_seat,
)

pytestmark = pytest.mark.unit

# make_team_world stubs the body loader; seat spawns here load the synthetic definitions for real.
_REAL_AGENT_BODY = _factory._load_agent_body
DEVELOPER = "fixture-worker"
REVIEWER = "fixture-definition-3d77e16a"


def _spawn_step(step_id: str, agent: str) -> PipelineStep:
    return PipelineStep(
        id=step_id,
        mcp={
            "server": "gobby-agents",
            "tool": "spawn_agent",
            "arguments": {"agent": agent},
        },
    )


RUNBOOK = PipelineDefinition(
    name="fixture-team-runbook",
    steps=[
        PipelineStep(id=ADMISSION_STEP, exec="true"),
        _spawn_step(DEVELOPER, DEVELOPER),
        _spawn_step(REVIEWER, REVIEWER),
    ],
    claim_flow={
        "implement": ClaimStage(seat=DEVELOPER, done_when=["recipe", "checks"], next="review"),
        "review": ClaimStage(seat=REVIEWER, done_when=["verdict"]),
    },
)


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
    # Each seat declares the kind it writes; a launched seat binds to its team by that kind.
    kinds = {DEVELOPER: "implementation", REVIEWER: "independent-review"}
    for name, kind in kinds.items():
        AgentDefinitionManager(temp_db).create(
            name,
            {
                "name": name,
                "version": "1.0",
                "prompts": {"agent": f"Run the {name} seat."},
                "workflows": {"rule_selectors": {"include": []}},
                "task_attestation_writes": ["*", kind],
            },
            project_id=world.case.project_id,
        )
    monkeypatch.setattr(_factory, "_load_agent_body", _REAL_AGENT_BODY)
    return world


async def _admit(world: TeamWorld, caller: Any, task: Any, **extra: Any) -> dict[str, Any]:
    """Call the admit_team tool as the runbook's admit step does."""
    arguments = {"task": f"#{task.seq_num}", **extra}
    with session_context_for_test(caller.id):
        reply: dict[str, Any] = await world.tools.call("admit_team", arguments)
    return reply


def _record_seat(world: TeamWorld, runbook: Any, step_id: str, seat: Any) -> None:
    """Record the seat step output the way the engine does after its spawn returns."""
    step = world.executions.create_step_execution(execution_id(runbook), step_id)
    world.executions.update_step_execution(
        step.id,
        status=StepStatus.COMPLETED,
        output_json=json.dumps({"child_session_id": seat.id}),
    )


def _end_seat(db: HubDatabase, seat: Any) -> None:
    db.execute("UPDATE sessions SET status = 'expired' WHERE id = %s", (seat.id,))
    db.execute("UPDATE agent_runs SET status = 'completed' WHERE id = %s", (seat.agent_run_id,))


def _attest(world: TeamWorld, task: Any, seat: Any, kind: str) -> None:
    record_attestation(
        world.case.db,
        task_id=task.id,
        session_id=seat.id,
        submission=AttestationSubmission(
            kind=kind,
            result="approved",
            summary="Checked the candidate.",
            reference=None if kind == "recipe" else {"scheme": "recipe", "value": "v1"},
            evidence={
                "stage": "test",
                "criteria": [{"criterion": "Works", "met": True, "how": "Ran it"}],
            },
        ),
    )


async def test_relaunched_developer_takes_the_claim_from_its_dead_predecessor(
    world: TeamWorld,
) -> None:
    db = world.case.db
    tasks = LocalTaskManager(db)
    task = world.case.task(TITLE)

    original = world.child(task, RUNBOOK)
    admitted = await _admit(world, original, task)
    assert admitted["success"] is True, admitted
    team = admitted["team"]
    developer = running_seat(world, original, DEVELOPER)
    reviewer = running_seat(world, original, REVIEWER)
    _record_seat(world, original, DEVELOPER, developer)
    _record_seat(world, original, REVIEWER, reviewer)
    # The spawn auto-claim gives the developer the task; it records part of the evidence.
    tasks.release_task_claim(task.id)
    tasks.claim_task(task.id, developer.id)
    _attest(world, task, developer, "recipe")
    _end_seat(db, developer)
    _end_seat(db, reviewer)
    world.finish(original)

    # The dev team relaunches through its admission: a new execution joins the same team.
    relaunch = world.child(task, RUNBOOK)
    relaunched = await _admit(world, relaunch, task, team_id=team["team_id"])
    assert relaunched["success"] is True, relaunched
    relaunched_developer = running_seat(world, relaunch, DEVELOPER)
    relaunched_reviewer = running_seat(world, relaunch, REVIEWER)
    _record_seat(world, relaunch, DEVELOPER, relaunched_developer)
    _record_seat(world, relaunch, REVIEWER, relaunched_reviewer)

    # The live developer records the implement evidence; no hand-off is made by hand.
    _attest(world, task, relaunched_developer, "recipe")
    _attest(world, task, relaunched_developer, "checks")

    assert tasks.get_task(task.id).claimed_by_session_id == relaunched_reviewer.id


async def _spawn_seat(
    world: TeamWorld, caller: Any, step_id: str, agent: str, team: dict[str, Any]
) -> str:
    """Spawn one runbook seat into the team tree; returns the run it launched."""
    reply = await world.spawn(
        caller,
        agent=agent,
        worktree_id=team["worktree_id"],
        checkout_mode="none",
        reserved_run_id=step_invocation_id(execution_id(caller), step_id),
    )
    assert reply["success"] is True, reply
    return str(reply["run_id"])


async def test_seat_spawns_bind_the_team_record_on_admit_and_relaunch(world: TeamWorld) -> None:
    db = world.case.db
    task = world.case.task(TITLE)

    original = world.child(task, RUNBOOK)
    admitted = await _admit(world, original, task)
    assert admitted["success"] is True, admitted
    team = admitted["team"]
    developer = await _spawn_seat(world, original, DEVELOPER, DEVELOPER, team)
    reviewer = await _spawn_seat(world, original, REVIEWER, REVIEWER, team)
    bound = team_binding(db, team["team_id"])
    assert bound is not None
    assert bound.primaries == {"fullstack": developer}
    assert bound.reviewers == {REVIEWER: reviewer}
    world.finish(original)

    # The relaunch replaces the bound runs: the team record names each live seat once.
    relaunch = world.child(task, RUNBOOK)
    rejoined = await _admit(world, relaunch, task, team_id=team["team_id"])
    assert rejoined["success"] is True, rejoined
    relaunched_developer = await _spawn_seat(world, relaunch, DEVELOPER, DEVELOPER, team)
    relaunched_reviewer = await _spawn_seat(world, relaunch, REVIEWER, REVIEWER, team)
    bound = team_binding(db, team["team_id"])
    assert bound is not None
    assert bound.primaries == {"fullstack": relaunched_developer}
    assert bound.reviewers == {REVIEWER: relaunched_reviewer}
