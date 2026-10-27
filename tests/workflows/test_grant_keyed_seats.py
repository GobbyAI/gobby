"""Reviewer and maker seat steps key on step grants, not definition names (addendum 7.1).

A docs-reviewer or research-reviewer seat runs the code reviewer's step workflow under its
own name, and a domain developer runs the developer's. Each seat is a synthetic copy of
that step workflow, installed under another name, with the grant under test.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

import pytest

from gobby.mcp_proxy.tools.task_commits import create_commit_registry
from gobby.mcp_proxy.tools.tasks._context import RegistryContext
from gobby.mcp_proxy.tools.tasks._session import create_session_registry
from gobby.storage.agents import LocalAgentRunManager
from gobby.storage.definitions.agents import AgentDefinitionManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.inter_session_messages import InterSessionMessageManager
from gobby.storage.project_checkouts import LocalProjectCheckoutManager
from gobby.storage.projects import LocalProjectManager
from gobby.storage.session_tasks import SessionTaskManager
from gobby.storage.tasks import LocalTaskManager
from gobby.utils.session_context import session_context_for_test
from gobby.workflows.engine.seat_transitions import candidate_binding_error
from gobby.workflows.state_manager import SessionVariableManager
from tests.fixtures.isolated_checkout import install_isolated_checkout_project
from tests.workflows.test_seat_step_loops import (
    _SYNTHETIC_SEATS,
    PEER_ID,
    PROJECT_ID,
    SESSION_ID,
    SHA,
    SeatRun,
    _git,
)

pytestmark = pytest.mark.unit

MACHINE_ID = "21000000-0000-4000-8000-000000000001"
COMMIT = (
    "-c",
    "user.name=Gobby Tests",
    "-c",
    "user.email=gobby-tests@example.com",
    "commit",
    "--allow-empty",
    "--no-gpg-sign",
    "-q",
    "-m",
)


def granted_seat(
    db: HubDatabase,
    template: str,
    name: str,
    grants: list[str],
    *,
    project_id: str = PROJECT_ID,
) -> SeatRun:
    """Run ``template``'s synthetic step workflow as definition ``name`` with ``grants``."""
    run = SeatRun(db, template, project_id=project_id)
    instance = run.instances.get_for_session(SESSION_ID)
    assert instance is not None
    # agent_name is identity: re-create the instance under the seat's own name.
    run.instances.delete_for_session(SESSION_ID)
    run.instances.save(instance.model_copy(update={"agent_name": name}))
    if name != template:
        body = json.loads(json.dumps(_SYNTHETIC_SEATS[template]))
        body.update(name=name, task_step_writes=grants)
        AgentDefinitionManager(db).create(name=name, definition_json=json.dumps(body), enabled=True)
    SessionVariableManager(db).set_variable(SESSION_ID, "_agent_type", name)
    run.session_vars["_agent_type"] = name
    return run


@pytest.mark.parametrize(
    ("name", "grants", "reviewer"),
    [
        ("docs-reviewer", ["review"], True),
        ("research-reviewer", ["review"], True),
        ("code-reviewer", ["review"], True),
        ("review-lookalike", [], False),
    ],
)
async def test_review_grant_drives_reviewer_steps(
    temp_db: HubDatabase, name: str, grants: list[str], reviewer: bool
) -> None:
    run = granted_seat(temp_db, "code-reviewer", name, grants)
    await run.load(*run.vars["required_skills"])
    task = run.task()

    # Verdict binding: candidate intake binds the unit, and the seat's own approval
    # at the bound tip readies the verdict.
    await run.intake(task)
    assert run.vars["candidate_received"] is reviewer
    if reviewer:
        assert run.step == "review"
        await run.approve(task)
        assert run.step == "verdict"
        assert run.vars["verdict_ready"] is True
    else:
        assert run.step != "review"

    # Intake explains a candidate it cannot bind only to a review-grant seat.
    forged = InterSessionMessageManager(temp_db).create_message(
        PEER_ID, SESSION_ID, f"EVENT=CANDIDATE TASK={task.id} RUN=run WT=tree COMMIT={SHA}"
    )
    explanation = candidate_binding_error(temp_db, SESSION_ID, forged)
    assert (explanation is not None) is reviewer, explanation


@pytest.mark.parametrize(
    ("name", "grants", "records_assignment"),
    [
        ("docs-reviewer", ["review"], True),
        ("research-reviewer", ["review"], True),
        ("frontend-developer", ["implementation"], False),
        ("review-lookalike", [], False),
    ],
)
async def test_reviewing_link_follows_the_review_grant(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    name: str,
    grants: list[str],
    records_assignment: bool,
) -> None:
    """The reviewing link accepts any running seat whose definition grants review."""
    monkeypatch.setattr("gobby.utils.machine_id._cached_machine_id", MACHINE_ID)
    LocalProjectCheckoutManager(temp_db).register(
        MACHINE_ID, sample_project["id"], str(tmp_path / "checkout")
    )
    AgentDefinitionManager(temp_db).create(
        name,
        {
            "name": name,
            "version": "1.0",
            "prompts": {"agent": "Review the candidate."},
            "workflows": {"rule_selectors": {"include": []}},
            "task_step_writes": grants,
        },
        project_id=sample_project["id"],
    )
    tasks = LocalTaskManager(temp_db)
    ctx = RegistryContext(task_manager=tasks)
    owner = ctx.session_manager.register(
        external_id="grant-owner",
        machine_id=MACHINE_ID,
        source="cli",
        project_id=sample_project["id"],
    )
    seat = ctx.session_manager.register(
        external_id=f"grant-{name}",
        machine_id=MACHINE_ID,
        source="cli",
        project_id=sample_project["id"],
    )
    SessionVariableManager(temp_db).set_variable(seat.id, "_agent_type", name)
    runs = LocalAgentRunManager(temp_db)
    run = runs.create(
        parent_session_id=owner.id,
        child_session_id=seat.id,
        provider="claude",
        agent_name=name,
        prompt="Review the candidate",
    )
    started = runs.start(run.id)
    assert started is not None and started.status == "running"
    bound = ctx.session_manager.update_terminal_pickup_metadata(seat.id, agent_run_id=run.id)
    assert bound is not None and bound.agent_run_id == run.id
    task = tasks.create_task(
        project_id=sample_project["id"],
        title="Reviewed candidate",
        created_in_session_id=owner.id,
        claimed_by_session_id=owner.id,
        validation_criteria="The verdict seat records its assignment.",
    )

    with session_context_for_test(seat.id):
        result = await create_session_registry(ctx).call(
            "link_task_to_session", {"task_id": task.id, "action": "reviewing"}
        )

    recorded = any(
        link["session_id"] == seat.id and link["action"] == "reviewing"
        for link in SessionTaskManager(temp_db).get_task_sessions(task.id)
    )
    assert recorded is records_assignment, result
    assert (result == {}) is records_assignment, result


@pytest.mark.parametrize(
    ("name", "grants", "maker"),
    [
        ("frontend-developer", ["implementation"], True),
        ("developer", ["implementation"], True),
        ("maker-lookalike", [], False),
    ],
)
async def test_implementation_grant_drives_maker_steps(
    temp_db: HubDatabase, name: str, grants: list[str], maker: bool
) -> None:
    """route_skills: reading the claimed task routes its extra skills for a maker."""
    run = granted_seat(temp_db, "developer", name, grants)
    await run.load(*run.vars["required_skills"])
    assert run.step == "claim"
    task = run.task(owner=SESSION_ID, skills=["typescript"])
    ref = f"#{task.seq_num}"
    await run.call(
        "gobby-tasks",
        "claim_task",
        {"task_id": task.id},
        {"result": {"task_id": task.id, "ref": ref}},
    )
    assert run.step == "route_skills"

    await run.call(
        "gobby-tasks", "get_task", {"task_id": task.id}, {"result": {"id": task.id, "ref": ref}}
    )
    if not maker:
        assert run.step == "route_skills"
        assert run.vars["skills_routed"] is False
        return
    assert run.step == "load_additional_skills"
    assert run.vars["additional_skills"] == ["typescript"]
    await run.load("typescript")
    assert run.step == "implement"


@pytest.mark.parametrize("name", ["frontend-developer", "developer"])
async def test_implementation_grant_drives_maker_submit_bounce(
    temp_db: HubDatabase, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    """The assigned reviewer's PROOF_REQUEST returns a submitted maker to implement."""
    checkout = install_isolated_checkout_project(
        temp_db,
        tmp_path / "repo",
        name="grant-bounce",
        machine_id=MACHINE_ID,
        monkeypatch=monkeypatch,
    )
    repo = Path(checkout.root_path)
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, *COMMIT, "linked candidate")
    sha = _git(repo, "rev-parse", "HEAD")
    run = granted_seat(
        temp_db, "developer", name, ["implementation"], project_id=checkout.project.id
    )
    await run.load(*run.vars["required_skills"])
    task = run.task(owner=SESSION_ID)
    await run.call(
        "gobby-tasks",
        "claim_task",
        {"task_id": task.id},
        {"result": {"task_id": task.id, "ref": f"#{task.seq_num}"}},
    )
    await run.call("gobby-tasks", "get_task", {"task_id": task.id}, {"result": {"id": task.id}})
    assert run.step == "implement"
    registry = create_commit_registry(
        task_manager=LocalTaskManager(temp_db),
        project_manager=LocalProjectManager(temp_db),
        session_manager=run.sessions,
    )
    arguments = {"task_id": task.id, "commit_sha": sha, "project_path": str(repo)}
    with session_context_for_test(SESSION_ID):
        output = cast(dict[str, Any], await registry.call("link_commit", arguments))
    SessionTaskManager(temp_db).link_task(PEER_ID, task.id, "reviewing")
    await run.call("gobby-tasks", "link_commit", arguments, output)
    assert run.step == "submit"
    await run.send(f"EVENT=CANDIDATE TASK=#{task.seq_num} RUN=run WT=tree COMMIT={sha} NOTE=ready")

    await run.incoming(f"EVENT=PROOF_REQUEST TASK={task.id} SHA={sha} NOTE=evidence", PEER_ID)
    assert run.step == "implement"
    assert run.vars["implementation_complete"] is False
