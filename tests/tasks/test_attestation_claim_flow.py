"""Pipeline policy moves claims inside the evidence transaction."""

import json
from dataclasses import asdict
from typing import Any
from uuid import UUID

import pytest

from gobby.agents.team_admission_records import TeamBinding
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.pipelines import LocalPipelineExecutionManager
from gobby.storage.sessions import SessionManager
from gobby.storage.tasks import LocalTaskManager
from gobby.tasks.attestations import (
    AttestationError,
    AttestationSubmission,
    list_attestations,
    record_attestation,
)
from gobby.utils.machine_id import require_machine_id
from gobby.workflows.pipeline_state import StepStatus
from tests.fixtures.agent_sessions import AgentSessions

pytestmark = pytest.mark.unit


def test_kind_results_return_declared_guidance(
    temp_db: HubDatabase, sample_project: dict[str, Any]
) -> None:
    author = AgentSessions(temp_db, sample_project["id"]).add(
        "baker",
        ["taste"],
        definition_fields={
            "task_attestation_kinds": {
                "taste": {
                    "allowed_results": ["approved", "rejected"],
                    "guidance": "Describe the tasting and use approved or rejected.",
                }
            }
        },
    )
    task = LocalTaskManager(temp_db).create_task(sample_project["id"], "Taste cake")
    with pytest.raises(AttestationError, match="Describe the tasting"):
        record_attestation(
            temp_db,
            task_id=task.id,
            session_id=author,
            submission=AttestationSubmission(kind="taste", result="passed", summary="Tasted it."),
        )
    assert list_attestations(temp_db, task.id) == []


def _exercise_claim_flow(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    recipient_state: str,
    tied_timestamps: bool,
) -> tuple[str, str]:
    project = sample_project["id"]
    tasks = LocalTaskManager(temp_db)
    task = tasks.create_task(project, "Build cake", validation_criteria="Cake tastes balanced.")
    seats = AgentSessions(temp_db, project)
    maker = seats.add("baker", ["recipe", "checks"])
    reviewer = seats.add("taster", ["verdict", "checks"])
    lead = seats.add("host", ["checks", "closing"])
    stages = {
        "implement": {"seat": "author", "done_when": ["recipe", "checks"], "next": "review"},
        "review": {
            "seat": "critic",
            "done_when": ["verdict", "checks"],
            "next": "validate",
            "on_rejected": "implement",
        },
        "validate": {"seat": "closer", "done_when": []},
    }
    executions = LocalPipelineExecutionManager(temp_db, project)
    execution = executions.create_execution(
        "cake-team",
        definition_json=json.dumps(
            {
                "name": "cake-team",
                "claim_flow": stages,
                "steps": [
                    {"id": "admit", "exec": "true"},
                    *[
                        {
                            "id": name,
                            "mcp": {
                                "server": "seats",
                                "tool": "spawn",
                                "arguments": {"agent": agent},
                            },
                        }
                        for name, agent in (
                            ("author", "baker"),
                            ("critic", "taster"),
                            ("closer", "host"),
                        )
                    ],
                ],
            }
        ),
    )
    parent = SessionManager(temp_db).register(
        external_id=f"pipeline-{execution.id}",
        source="pipeline",
        machine_id=require_machine_id(),
        project_id=project,
    )
    binding = TeamBinding(execution.id, task.id, project, "cake", "main", None, None)
    for name, output in (
        ("admit", {"team": asdict(binding)}),
        ("author", {"child_session_id": maker}),
        ("critic", {"child_session_id": reviewer}),
        ("closer", {"child_session_id": lead}),
    ):
        step = executions.create_step_execution(execution.id, name)
        executions.update_step_execution(
            step.id, status=StepStatus.COMPLETED, output_json=json.dumps(output)
        )
    for session in (maker, reviewer, lead):
        temp_db.execute(
            "UPDATE sessions SET parent_session_id = %s WHERE id = %s", (parent.id, session)
        )
    tasks.claim_task(task.id, maker)

    if recipient_state != "original":
        temp_db.execute("UPDATE sessions SET status = 'expired' WHERE id = %s", (reviewer,))
    if recipient_state == "replacement":
        from gobby.storage.agents import LocalAgentRunManager

        seats.next_seq = parent.seq_num or seats.next_seq
        reviewer = seats.add("taster", ["verdict", "checks"])
        temp_db.execute(
            "UPDATE sessions SET parent_session_id = %s WHERE id = %s", (parent.id, reviewer)
        )
        runs = LocalAgentRunManager(temp_db)
        run = runs.create(
            parent.id, "codex", "Taste again", agent_name="taster", child_session_id=reviewer
        )
        runs.start(run.id)

    def attest(author: str, kind: str, result: str = "approved") -> None:
        record_attestation(
            temp_db,
            task_id=task.id,
            session_id=author,
            submission=AttestationSubmission(
                kind=kind,
                result=result,
                summary="Checked the cake.",
                reference={"scheme": "recipe", "value": "v1"},
                evidence={
                    "stage": "test",
                    "criteria": [{"criterion": "Balanced taste", "met": True, "how": "Tasted"}],
                },
            ),
        )

        if tied_timestamps:
            temp_db.execute(
                "UPDATE task_comments SET created_at = %s WHERE task_id = %s AND author_type = %s",
                ("2026-01-01T00:00:00+00:00", task.id, "task_claim_flow"),
            )

    attest(maker, "recipe")
    assert tasks.get_task(task.id).claimed_by_session_id == maker
    if recipient_state == "missing":
        count = len(list_attestations(temp_db, task.id))
        with pytest.raises(ValueError, match="live pipeline seat"):
            attest(maker, "checks")
        assert tasks.get_task(task.id).claimed_by_session_id == maker
        assert len(list_attestations(temp_db, task.id)) == count
        return task.id, maker
    attest(maker, "checks")
    assert tasks.get_task(task.id).claimed_by_session_id == reviewer
    attest(reviewer, "verdict", "rejected")
    assert tasks.get_task(task.id).claimed_by_session_id == maker
    attest(maker, "recipe")
    assert tasks.get_task(task.id).claimed_by_session_id == maker
    attest(maker, "checks")
    assert tasks.get_task(task.id).claimed_by_session_id == reviewer
    attest(reviewer, "verdict")
    count = len(list_attestations(temp_db, task.id))
    with pytest.raises(RuntimeError, match="rollback"):
        with temp_db.transaction():
            attest(reviewer, "checks")
            assert tasks.get_task(task.id).claimed_by_session_id == lead
            raise RuntimeError("rollback")
    assert tasks.get_task(task.id).claimed_by_session_id == reviewer
    assert len(list_attestations(temp_db, task.id)) == count
    attest(reviewer, "checks")
    assert tasks.get_task(task.id).claimed_by_session_id == lead
    return task.id, lead


@pytest.mark.parametrize("recipient_state", ["original", "replacement", "missing"])
@pytest.mark.parametrize("tied_timestamps", [False, True])
def test_claim_flow_advances_rejects_and_rolls_back_atomically(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    recipient_state: str,
    tied_timestamps: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if tied_timestamps:
        identifiers = iter(UUID(int=100 - index) for index in range(20))
        monkeypatch.setattr("gobby.tasks.claim_flow.uuid4", lambda: next(identifiers))
    task_id, expected = _exercise_claim_flow(
        temp_db, sample_project, recipient_state, tied_timestamps
    )
    assert LocalTaskManager(temp_db).get_task(task_id).claimed_by_session_id == expected
