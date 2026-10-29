"""A researcher seat closes its own research spike once its report is committed."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from gobby.mcp_proxy.tools.tasks import create_task_registry
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.sessions import SessionManager
from gobby.storage.tasks import LocalTaskManager
from gobby.tasks.step_records import (
    StepRecordError,
    StepSubmission,
    TaskStep,
    list_task_step_records,
    record_task_step,
    task_step_requirements,
)
from gobby.utils.machine_id import require_machine_id
from gobby.utils.session_context import session_context_for_test
from gobby.workflows.agent_resolver import session_agent_definition
from gobby.workflows.state_manager import SessionVariableManager
from tests.fixtures.agent_definitions import seed_seat
from tests.tasks.test_close_invariants import _set_close_policy, git
from tests.tasks.test_step_records import _Seats

pytestmark = pytest.mark.unit


@pytest.mark.asyncio
async def test_researcher_records_its_spike_steps_and_closes_it(
    temp_db: HubDatabase, sample_git_project: dict[str, Any]
) -> None:
    project_id = sample_git_project["id"]
    repo = Path(sample_git_project["repo_path"])
    _set_close_policy(temp_db, project_id, {"enforce_step_records": True})
    seed_seat(
        temp_db,
        "researcher",
        task_step_writes=["implementation", "closing"],
        task_step_scope={"task_types": ["research_spike"], "own_claim_only": True},
        task_close_scope={
            "task_types": ["research_spike"],
            "categories": ["research"],
            "own_claim_only": True,
        },
        task_close_reasons=["done"],
        required_review_capabilities=["approve_report_review"],
    )
    seed_seat(
        temp_db, "orchestrator", task_step_writes=["review"], capabilities=["approve_report_review"]
    )
    sessions = SessionManager(temp_db)
    session = sessions.register(
        external_id="researcher-spike-close",
        machine_id=require_machine_id(),
        source="claude",
        project_id=project_id,
    )
    SessionVariableManager(temp_db).set_variable(session.id, "_agent_type", "researcher")
    definition = session_agent_definition(temp_db, session)
    assert definition is not None and definition[0].name == "researcher"

    manager = LocalTaskManager(temp_db)
    task = manager.create_task(
        project_id,
        "Research the debt",
        task_type="research_spike",
        category="research",
        validation_criteria="The committed report answers the question.",
    )
    manager.claim_task(task.id, session.id)
    report = repo / ".gobby/plans/research/debt.md"
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text("# Debt report\n")
    git(repo, "add", str(report))
    git(repo, "commit", "-m", "research report")
    sha = git(repo, "rev-parse", "HEAD")

    registry = create_task_registry(manager)
    with session_context_for_test(session.id):
        linked = await registry.call("link_commit", {"task_id": task.id, "commit_sha": sha})
        assert "error" not in linked, linked
        task = manager.get_task(task.id)
        required = task_step_requirements(temp_db, task).required
        assert required, "the research spike policy requires step records"
        assert "review" in required, "a research spike needs an independent review record"
        for step in (step for step in required if step != "review"):
            recorded = await registry.call(
                f"record_{step}_step",
                {
                    "task_id": task.id,
                    "status": "approved",
                    "epoch": task.review_epoch,
                    "round_number": 1,
                    "feedback": f"The researcher's {step} step: report committed.",
                    "commit_sha": sha,
                },
            )
            assert "error" not in recorded, (step, recorded)

        refused = await registry.call(
            "record_review_step",
            {
                "task_id": task.id,
                "status": "approved",
                "epoch": task.review_epoch,
                "round_number": 1,
                "feedback": "A researcher approving its own spike.",
                "commit_sha": sha,
            },
        )
        assert refused == {"error": "agent definition researcher cannot write the review step"}

        unreviewed = await registry.call("close_task", {"task_id": task.id, "commit_sha": sha})

    # Without the Orchestrator's approval, gate 5 names review and the spike stays open.
    assert unreviewed.get("closed") is not True, unreviewed
    (review_gate,) = (gate for gate in unreviewed["gates"] if gate["item"] == 5)
    assert review_gate["status"] == "failed"
    assert "review" in review_gate["message"]
    assert manager.get_task(task.id).closed_at is None

    orchestrator = sessions.register(
        external_id="orchestrator-spike-close",
        machine_id=require_machine_id(),
        source="claude",
        project_id=project_id,
    )
    SessionVariableManager(temp_db).set_variable(orchestrator.id, "_agent_type", "orchestrator")
    with session_context_for_test(orchestrator.id):
        reviewed = await registry.call(
            "record_review_step",
            {
                "task_id": task.id,
                "status": "approved",
                "epoch": task.review_epoch,
                "round_number": 1,
                "feedback": "The report answers the question and its claims match the commit.",
                "commit_sha": sha,
            },
        )
    assert "error" not in reviewed, reviewed

    with session_context_for_test(session.id):
        closed = await registry.call("close_task", {"task_id": task.id, "commit_sha": sha})

    assert closed.get("closed") is True, closed
    assert len(closed["gates"]) == 5
    assert all(gate["status"] == "passed" for gate in closed["gates"]), closed["gates"]
    assert manager.get_task(task.id).closed_at is not None


def _step_gate(result: dict[str, Any]) -> dict[str, Any]:
    gates: list[dict[str, Any]] = result["gates"]
    (gate,) = (gate for gate in gates if gate["item"] == 5)
    return gate


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "policy",
    [
        {"enforce_step_records": False},
        {"enforce_step_records": True, "review_categories": []},
    ],
    ids=["step_records_off", "research_not_reviewed"],
)
async def test_researcher_spike_close_needs_orchestrator_review_under_any_policy(
    temp_db: HubDatabase, sample_git_project: dict[str, Any], policy: dict[str, Any]
) -> None:
    project_id = sample_git_project["id"]
    repo = Path(sample_git_project["repo_path"])
    _set_close_policy(temp_db, project_id, policy)
    seed_seat(
        temp_db,
        "researcher",
        task_step_writes=["implementation", "closing"],
        task_step_scope={"task_types": ["research_spike"], "own_claim_only": True},
        task_close_scope={
            "task_types": ["research_spike"],
            "categories": ["research"],
            "own_claim_only": True,
        },
        task_close_reasons=["done"],
        required_review_capabilities=["approve_report_review"],
    )
    seed_seat(
        temp_db, "orchestrator", task_step_writes=["review"], capabilities=["approve_report_review"]
    )
    sessions = SessionManager(temp_db)
    session = sessions.register(
        external_id="researcher-policy-close",
        machine_id=require_machine_id(),
        source="claude",
        project_id=project_id,
    )
    SessionVariableManager(temp_db).set_variable(session.id, "_agent_type", "researcher")
    manager = LocalTaskManager(temp_db)
    task = manager.create_task(
        project_id,
        "Research the debt",
        task_type="research_spike",
        category="research",
        validation_criteria="The committed report answers the question.",
    )
    manager.claim_task(task.id, session.id)
    report = repo / ".gobby/plans/research/debt.md"
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text("# Debt report\n")
    git(repo, "add", str(report))
    git(repo, "commit", "-m", "research report")
    sha = git(repo, "rev-parse", "HEAD")

    registry = create_task_registry(manager)
    with session_context_for_test(session.id):
        linked = await registry.call("link_commit", {"task_id": task.id, "commit_sha": sha})
        assert "error" not in linked, linked
        task = manager.get_task(task.id)
        definition = session_agent_definition(temp_db, session)
        assert definition is not None
        required = task_step_requirements(temp_db, task, closer_definition=definition[0]).required
        assert "review" in required, "a researcher's spike needs the Orchestrator's review"
        for step in (step for step in required if step != "review"):
            recorded = await registry.call(
                f"record_{step}_step",
                {
                    "task_id": task.id,
                    "status": "approved",
                    "epoch": task.review_epoch,
                    "round_number": 1,
                    "feedback": f"The researcher's {step} step: report committed.",
                    "commit_sha": sha,
                },
            )
            assert "error" not in recorded, (step, recorded)

        refused_preview = await registry.call(
            "close_task", {"task_id": task.id, "commit_sha": sha, "preview": True}
        )
        refused = await registry.call("close_task", {"task_id": task.id, "commit_sha": sha})

    assert refused_preview.get("can_close") is False, refused_preview
    assert refused.get("closed") is not True, refused
    for result in (refused_preview, refused):
        assert _step_gate(result)["status"] == "failed", result
        assert "review" in _step_gate(result)["message"], result
    assert manager.get_task(task.id).closed_at is None

    orchestrator = sessions.register(
        external_id="orchestrator-policy-close",
        machine_id=require_machine_id(),
        source="claude",
        project_id=project_id,
    )
    SessionVariableManager(temp_db).set_variable(orchestrator.id, "_agent_type", "orchestrator")
    with session_context_for_test(orchestrator.id):
        reviewed = await registry.call(
            "record_review_step",
            {
                "task_id": task.id,
                "status": "approved",
                "epoch": task.review_epoch,
                "round_number": 1,
                "feedback": "The report answers the question and its claims match the commit.",
                "commit_sha": sha,
            },
        )
    assert "error" not in reviewed, reviewed

    with session_context_for_test(session.id):
        allowed_preview = await registry.call(
            "close_task", {"task_id": task.id, "commit_sha": sha, "preview": True}
        )
        closed = await registry.call("close_task", {"task_id": task.id, "commit_sha": sha})

    assert allowed_preview.get("can_close") is True, allowed_preview
    assert closed.get("closed") is True, closed
    assert _step_gate(closed)["status"] == "passed", closed
    assert manager.get_task(task.id).closed_at is not None


@pytest.mark.parametrize("target", ["code_task", "foreign_spike"])
@pytest.mark.parametrize("step", ["implementation", "closing"])
def test_researcher_records_only_its_own_research_spike(
    temp_db: HubDatabase, sample_project: dict[str, Any], target: str, step: TaskStep
) -> None:
    project_id = sample_project["id"]
    seats = _Seats(temp_db, project_id)
    researcher = seats.add(
        "researcher",
        ["implementation", "closing"],
        definition_fields={
            "task_step_scope": {"task_types": ["research_spike"], "own_claim_only": True}
        },
    )
    other = seats.add("researcher-peer", ["implementation", "closing"])
    manager = LocalTaskManager(temp_db)
    if target == "code_task":
        task = manager.create_task(
            project_id, "Code work", category="code", validation_criteria="Code ships."
        )
        manager.claim_task(task.id, researcher)
    else:
        task = manager.create_task(
            project_id,
            "Another researcher's spike",
            task_type="research_spike",
            category="research",
            validation_criteria="The report answers the question.",
        )
        manager.claim_task(task.id, other)

    with pytest.raises(StepRecordError):
        record_task_step(
            temp_db,
            task_id=task.id,
            session_id=researcher,
            step=step,
            submission=StepSubmission(
                status="approved",
                epoch=1,
                round_number=1,
                feedback=f"A researcher's {step} step outside its spike.",
                disposition="moot",
            ),
        )
    assert list_task_step_records(temp_db, task.id) == []
