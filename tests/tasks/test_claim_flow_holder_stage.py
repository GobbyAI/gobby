"""Claim-flow regressions for #25795.

Each test runs the installed claim_flow of the first bundled pipeline that declares one, so
stage names, seats and done_when kinds come from YAML. A direct claim or an explicit hand-off
changes the claim holder without writing a claim-flow marker; the marker's stage then names a
seat the holder no longer is, and the holder's complete evidence must still move the claim.
"""

import json
from dataclasses import asdict, dataclass
from typing import Any

import pytest
import yaml

from gobby.agents.team_admission_records import TeamBinding
from gobby.storage.agents import LocalAgentRunManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.pipelines import LocalPipelineExecutionManager
from gobby.storage.sessions import SessionManager
from gobby.storage.tasks import LocalTaskManager
from gobby.tasks.attestations import AttestationSubmission, list_attestations, record_attestation
from gobby.utils.machine_id import require_machine_id
from gobby.workflows.pipeline_models import PipelineDefinition
from gobby.workflows.pipeline_state import StepStatus
from gobby.workflows.sync_pipelines import get_bundled_pipelines_path
from tests.fixtures.agent_sessions import AgentSessions

pytestmark = pytest.mark.unit

C1 = "dbadb7a951887b0738eab0918879a82af07be6c4"
C2 = "ab" * 20


def _claim_pipeline() -> dict[str, Any]:
    for path in sorted(get_bundled_pipelines_path().glob("*.yaml")):
        raw: dict[str, Any] = yaml.safe_load(path.read_text())
        if raw.get("claim_flow"):
            return raw
    raise AssertionError("no bundled pipeline declares claim_flow")


@dataclass
class _Team:
    db: HubDatabase
    definition: PipelineDefinition
    task_id: str
    sessions: dict[str, str]
    implement: str
    review: str
    validate: str

    def holder(self) -> str:
        return str(LocalTaskManager(self.db).get_task(self.task_id).claimed_by_session_id)

    def hand_off(self, recipient: str, holder: str) -> None:
        """The explicit transfer a coordinator makes with hand_off_task_claim."""
        LocalTaskManager(self.db).claim_task_for_agent(
            self.task_id, recipient, expected_owner=holder, handed=False
        )

    def attest(
        self,
        session: str,
        kind: str,
        stage: str,
        *,
        result: str = "approved",
        sha: str | None = None,
        supersedes_id: str | None = None,
    ) -> str:
        record, _ = record_attestation(
            self.db,
            task_id=self.task_id,
            session_id=session,
            submission=AttestationSubmission(
                kind=kind,
                result=result,
                summary=f"probe {kind} {result}",
                reference={"scheme": "git", "value": sha} if sha else None,
                supersedes_id=supersedes_id,
                evidence={
                    "stage": stage,
                    "criteria": [{"criterion": "Works.", "met": True, "how": "probe"}],
                },
            ),
        )
        return record.id

    def chain(
        self, session: str, stage: str, sha: str, *, first_reference: str | None = None
    ) -> list[str]:
        """Records every done_when kind of a stage on one candidate, in declared order."""
        ids = []
        for index, kind in enumerate(self.definition.claim_flow[stage].done_when):
            reference = first_reference if index == 0 else sha
            ids.append(self.attest(session, kind, stage, sha=reference))
        return ids


def _team(db: HubDatabase, sample_project: dict[str, Any]) -> _Team:
    definition = PipelineDefinition.model_validate(_claim_pipeline())
    project = sample_project["id"]
    task = LocalTaskManager(db).create_task(project, "Probe", validation_criteria="Works.")
    seat_ids = {stage.seat for stage in definition.claim_flow.values()}
    agent_of: dict[str, str] = {
        step.id: (step.mcp.arguments or {})["agent"]
        for step in definition.steps
        if step.mcp and step.id in seat_ids
    }
    seats = AgentSessions(db, project)
    seat_sid: dict[str, str] = {}
    for stage in definition.claim_flow.values():
        if stage.seat in seat_sid:
            continue
        kinds = [
            kind
            for other in definition.claim_flow.values()
            if other.seat == stage.seat
            for kind in other.done_when
        ]
        seat_sid[stage.seat] = seats.add(agent_of[stage.seat], kinds)
    executions = LocalPipelineExecutionManager(db, project)
    execution = executions.create_execution(
        definition.name, definition_json=definition.model_dump_json()
    )
    parent = SessionManager(db).register(
        external_id=f"pipeline-{execution.id}",
        source="pipeline",
        machine_id=require_machine_id(),
        project_id=project,
    )
    binding = TeamBinding(execution.id, task.id, project, "single", "main", None, None)
    step = executions.create_step_execution(execution.id, "admit")
    executions.update_step_execution(
        step.id,
        status=StepStatus.COMPLETED,
        output_json=json.dumps({"team": asdict(binding)}),
    )
    for seat, sid in seat_sid.items():
        step = executions.create_step_execution(execution.id, seat)
        executions.update_step_execution(
            step.id,
            status=StepStatus.COMPLETED,
            output_json=json.dumps({"child_session_id": sid}),
        )
        db.execute("UPDATE sessions SET parent_session_id = %s WHERE id = %s", (parent.id, sid))
    runs = LocalAgentRunManager(db)
    for seat, sid in seat_sid.items():
        run = runs.create(
            parent.id, "claude", f"Seat {seat}", agent_name=agent_of[seat], child_session_id=sid
        )
        runs.start(run.id)
    db.execute("UPDATE sessions SET status = 'deleted' WHERE id = %s", (parent.id,))

    targets = {stage.next for stage in definition.claim_flow.values()}
    implement = next(name for name in definition.claim_flow if name not in targets)
    review = definition.claim_flow[implement].next
    assert review is not None
    validate = definition.claim_flow[review].next
    assert validate is not None
    sessions = {name: seat_sid[definition.claim_flow[name].seat] for name in definition.claim_flow}
    LocalTaskManager(db).claim_task(task.id, sessions[implement])
    return _Team(db, definition, task.id, sessions, implement, review, validate)


def test_re_candidate_after_explicit_hand_off_moves_to_reviewer(
    temp_db: HubDatabase, sample_project: dict[str, Any]
) -> None:
    team = _team(temp_db, sample_project)
    implementer, reviewer = team.sessions[team.implement], team.sessions[team.review]
    team.chain(implementer, team.implement, C1)
    assert team.holder() == reviewer

    team.hand_off(recipient=implementer, holder=reviewer)
    team.chain(implementer, team.implement, C2)

    assert team.holder() == reviewer


def test_superseding_test_before_then_re_recorded_chain_moves_to_reviewer(
    temp_db: HubDatabase, sample_project: dict[str, Any]
) -> None:
    team = _team(temp_db, sample_project)
    implementer, reviewer = team.sessions[team.implement], team.sessions[team.review]
    first = team.chain(implementer, team.implement, C1)
    assert team.holder() == reviewer

    team.hand_off(recipient=implementer, holder=reviewer)
    team.attest(implementer, "test-before", team.implement, supersedes_id=first[0])
    assert team.holder() == implementer
    for kind in team.definition.claim_flow[team.implement].done_when[1:]:
        team.attest(implementer, kind, team.implement, sha=C2)

    assert team.holder() == reviewer


def test_review_chain_moves_to_lead_after_explicit_hand_off_to_reviewer(
    temp_db: HubDatabase, sample_project: dict[str, Any]
) -> None:
    team = _team(temp_db, sample_project)
    implementer, reviewer = team.sessions[team.implement], team.sessions[team.review]
    lead = team.sessions[team.validate]
    team.chain(implementer, team.implement, C1)
    team.attest(reviewer, "independent-review", team.review, result="rejected", sha=C1)
    assert team.holder() == implementer

    team.hand_off(recipient=reviewer, holder=implementer)
    team.chain(implementer, team.implement, C2)
    assert team.holder() == reviewer
    team.chain(reviewer, team.review, C2, first_reference=C2)

    assert team.holder() == lead


def test_incomplete_implement_chain_does_not_move(
    temp_db: HubDatabase, sample_project: dict[str, Any]
) -> None:
    team = _team(temp_db, sample_project)
    implementer, reviewer = team.sessions[team.implement], team.sessions[team.review]
    kinds = team.definition.claim_flow[team.implement].done_when
    for index, kind in enumerate(kinds[:-1]):
        team.attest(implementer, kind, team.implement, sha=None if index == 0 else C1)

    assert team.holder() == implementer
    assert reviewer != implementer


def test_superseded_evidence_alone_does_not_move(
    temp_db: HubDatabase, sample_project: dict[str, Any]
) -> None:
    team = _team(temp_db, sample_project)
    implementer, reviewer = team.sessions[team.implement], team.sessions[team.review]
    first = team.chain(implementer, team.implement, C1)
    team.attest(reviewer, "independent-review", team.review, result="rejected", sha=C1)
    assert team.holder() == implementer

    team.attest(implementer, "validation", team.implement, sha=C1, supersedes_id=first[-1])

    assert team.holder() == implementer


def test_evidence_for_a_different_sha_refuses_and_does_not_move(
    temp_db: HubDatabase, sample_project: dict[str, Any]
) -> None:
    team = _team(temp_db, sample_project)
    implementer = team.sessions[team.implement]
    kinds = team.definition.claim_flow[team.implement].done_when
    for index, kind in enumerate(kinds[:-1]):
        team.attest(implementer, kind, team.implement, sha=None if index == 0 else C1)
    count = len(list_attestations(temp_db, team.task_id))

    with pytest.raises(ValueError, match="requires one reference"):
        team.attest(implementer, kinds[-1], team.implement, sha=C2)

    assert team.holder() == implementer
    assert len(list_attestations(temp_db, team.task_id)) == count


def test_rejection_returns_claim_and_corrected_chain_moves_to_reviewer(
    temp_db: HubDatabase, sample_project: dict[str, Any]
) -> None:
    team = _team(temp_db, sample_project)
    implementer, reviewer = team.sessions[team.implement], team.sessions[team.review]
    team.chain(implementer, team.implement, C1)
    team.attest(reviewer, "independent-review", team.review, result="rejected", sha=C1)
    assert team.holder() == implementer

    team.chain(implementer, team.implement, C2)

    assert team.holder() == reviewer
