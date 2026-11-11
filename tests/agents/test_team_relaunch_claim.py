"""A relaunched team seat takes over the claim its dead predecessor held (#25719).

Synthetic definitions only: the seat agents and the claim_flow stages are fixture names.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

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
from tests.agents.test_team_epic_dispatch import epic_team, new_task
from tests.fixtures.landing_case import make_landing_case
from tests.fixtures.team_case import (
    TITLE,
    TeamWorld,
    execution_id,
    make_team_world,
    running_seat,
    synthetic_manager_body,
)

pytestmark = pytest.mark.unit

# make_team_world stubs the body loader; seat spawns here load the synthetic definitions for real.
_REAL_AGENT_BODY = _factory._load_agent_body
DEVELOPER = "fixture-worker"
REVIEWER = "fixture-definition-3d77e16a"
MANAGER = "fixture-coordinator"


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


LEAF_RUNBOOK = PipelineDefinition(
    name="fixture-leaf-runbook",
    steps=[
        PipelineStep(id=ADMISSION_STEP, exec="true"),
        _spawn_step(MANAGER, MANAGER),
        _spawn_step(REVIEWER, REVIEWER),
    ],
    claim_flow={
        "review": ClaimStage(seat=REVIEWER, done_when=["independent-review"], next="validate"),
        "validate": ClaimStage(seat=MANAGER, done_when=["validation"]),
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
    manager_body = {**synthetic_manager_body(), "name": MANAGER}
    AgentDefinitionManager(temp_db).create(MANAGER, manager_body, project_id=world.case.project_id)
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


async def test_relaunched_seat_takes_the_claim_from_its_dead_predecessor(
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
    # The spawn auto-claim gives the first seat the task; it records part of the evidence.
    tasks.release_task_claim(task.id)
    tasks.claim_task(task.id, developer.id)
    _attest(world, task, developer, "recipe")
    _end_seat(db, developer)
    _end_seat(db, reviewer)
    world.finish(original)

    # The team relaunches through its admission: a new execution joins the same team.
    relaunch = world.child(task, RUNBOOK)
    relaunched = await _admit(world, relaunch, task, team_id=team["team_id"])
    assert relaunched["success"] is True, relaunched
    relaunched_developer = running_seat(world, relaunch, DEVELOPER)
    relaunched_reviewer = running_seat(world, relaunch, REVIEWER)
    _record_seat(world, relaunch, DEVELOPER, relaunched_developer)
    _record_seat(world, relaunch, REVIEWER, relaunched_reviewer)

    # The live seat records the implement evidence; no hand-off is made by hand.
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


async def _pod_awaiting_validation(world: TeamWorld) -> tuple[Any, Any, str]:
    """A leaf pod whose manager step names an ended session, under a live epic manager.

    Returns the leaf, the pod's reviewer holding its claim, and the parent team's manager.
    """
    db = world.case.db
    epic = new_task(world, "Parent epic", epic=True)
    parent = await epic_team(world, epic, manager_name=MANAGER)
    leaf = new_task(world, "Leaf under the epic", parent=epic)
    pod_run = world.child(leaf, LEAF_RUNBOOK, parent=parent.manager)
    await world.admit(pod_run, leaf, parent_team_id=parent.binding.team_id)
    ended = world.case.session(MANAGER, parent=pod_run.id)
    db.execute("UPDATE sessions SET status = 'expired' WHERE id = %s", (ended,))
    step = world.executions.create_step_execution(execution_id(pod_run), MANAGER)
    world.executions.update_step_execution(
        step.id,
        status=StepStatus.COMPLETED,
        output_json=json.dumps({"child_session_id": ended}),
    )
    reviewer = running_seat(world, pod_run, REVIEWER)
    LocalTaskManager(db).claim_task(leaf.id, reviewer.id)
    return leaf, reviewer, parent.manager


async def test_pod_validation_moves_the_claim_to_the_live_parent_manager(
    world: TeamWorld,
) -> None:
    leaf, reviewer, manager = await _pod_awaiting_validation(world)
    _attest(world, leaf, reviewer, "independent-review")
    assert LocalTaskManager(world.case.db).get_task(leaf.id).claimed_by_session_id == manager


async def test_pod_validation_is_refused_without_a_live_manager_on_either_team(
    world: TeamWorld,
) -> None:
    leaf, reviewer, manager = await _pod_awaiting_validation(world)
    db = world.case.db
    db.execute("UPDATE sessions SET status = 'expired' WHERE id = %s", (manager,))
    db.execute("UPDATE agent_runs SET status = 'completed' WHERE child_session_id = %s", (manager,))
    with pytest.raises(ValueError, match="needs its live pipeline seat"):
        _attest(world, leaf, reviewer, "independent-review")
    assert LocalTaskManager(world.case.db).get_task(leaf.id).claimed_by_session_id == reviewer.id
