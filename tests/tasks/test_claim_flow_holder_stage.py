"""Claim-flow holder regressions for #25795.

Each test runs a synthetic claim_flow declared inline: stage names, seats and done_when kinds
come from the test, never from an installed pipeline. A direct claim or an explicit hand-off
changes the claim holder without writing a claim-flow marker; the holder's complete evidence must
still move the claim, a relaunched seat must not strand it, and evidence from a session outside
the flow must be refused rather than ignored.
"""

import json
from dataclasses import asdict, dataclass
from typing import Any

import pytest

from gobby.agents.team_admission_records import TeamBinding
from gobby.storage.agents import LocalAgentRunManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.pipelines import LocalPipelineExecutionManager
from gobby.storage.sessions import SessionManager
from gobby.storage.tasks import LocalTaskManager
from gobby.storage.team_records import ADMISSION_STEP
from gobby.tasks.attestations import AttestationSubmission, list_attestations, record_attestation
from gobby.utils.machine_id import require_machine_id
from gobby.workflows.pipeline_models import (
    ClaimStage,
    MCPStepConfig,
    PipelineDefinition,
    PipelineStep,
)
from gobby.workflows.pipeline_state import StepStatus
from tests.fixtures.agent_sessions import AgentSessions

pytestmark = pytest.mark.unit

C1 = "dbadb7a951887b0738eab0918879a82af07be6c4"
C2 = "ab" * 20
IMPLEMENT = "implement"
REVIEW = "review"
VALIDATE = "validate"
AGENTS = {"build": "builder-agent", "check": "checker-agent", "close": "closer-agent"}
DOC_WRITER = "writer-agent"
DOC_REVIEWER = "reviewer-agent"
REVIEW_DOCS = "review-docs"


def _flow(closer_seat: str = "close") -> PipelineDefinition:
    """Build implements, check reviews, and closer_seat validates and closes."""
    seats = sorted({"build", "check", closer_seat})
    admit = PipelineStep(
        id=ADMISSION_STEP, mcp=MCPStepConfig(server="synthetic", tool="admit_team")
    )
    return PipelineDefinition(
        name="synthetic-claim-flow",
        steps=[
            admit,
            *(
                PipelineStep(
                    id=seat,
                    mcp=MCPStepConfig(
                        server="synthetic", tool="spawn_agent", arguments={"agent": AGENTS[seat]}
                    ),
                )
                for seat in seats
            ),
        ],
        claim_flow={
            IMPLEMENT: ClaimStage(
                seat="build",
                done_when=[
                    "test-before",
                    "implementation",
                    "test-after",
                    "self-review",
                    "validation",
                ],
                next=REVIEW,
            ),
            REVIEW: ClaimStage(
                seat="check",
                done_when=["independent-review", "validation"],
                next=VALIDATE,
                on_rejected=IMPLEMENT,
            ),
            VALIDATE: ClaimStage(seat=closer_seat, done_when=["validation", "closing"]),
        },
    )


def _seat_key(stage: ClaimStage) -> str:
    """Session key for a stage: its pipeline seat, or the agent it names outside the steps."""
    key = stage.seat if stage.seat is not None else stage.agent
    assert key is not None
    return key


def _agent_name(stage: ClaimStage) -> str:
    if stage.agent is not None:
        return stage.agent
    assert stage.seat is not None
    return AGENTS[stage.seat]


def _seat_kinds(definition: PipelineDefinition, key: str) -> list[str]:
    kinds = [
        kind
        for stage in definition.claim_flow.values()
        if _seat_key(stage) == key
        for kind in stage.done_when
    ]
    return list(dict.fromkeys(kinds))


def _docs_flow() -> PipelineDefinition:
    """A docs task: the writer and reviewer are agents the lead spawns, not pipeline steps."""
    definition = _flow()
    base = definition.claim_flow
    return definition.model_copy(
        update={
            "claim_flow": {
                IMPLEMENT: ClaimStage(
                    agent=DOC_WRITER, done_when=base[IMPLEMENT].done_when, next=REVIEW
                ),
                REVIEW: ClaimStage(
                    agent=DOC_REVIEWER,
                    done_when=["independent-review"],
                    next=VALIDATE,
                    on_rejected=IMPLEMENT,
                ),
                VALIDATE: base[VALIDATE],
            }
        }
    )


@dataclass
class _Team:
    db: HubDatabase
    definition: PipelineDefinition
    task_id: str
    parent: str
    seats: AgentSessions
    sessions: dict[str, str]
    agents: dict[str, str]

    def session(self, stage: str) -> str:
        return self.sessions[_seat_key(self.definition.claim_flow[stage])]

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

    def relaunch(self, stage: str) -> str:
        """Starts a new live session for the stage's seat and ends the old seat's run."""
        key = _seat_key(self.definition.claim_flow[stage])
        fresh = self.seats.add(self.agents[key], _seat_kinds(self.definition, key))
        self.db.execute(
            "UPDATE sessions SET parent_session_id = %s WHERE id = %s", (self.parent, fresh)
        )
        self.db.execute(
            "UPDATE agent_runs SET status = 'completed' WHERE child_session_id = %s",
            (self.sessions[key],),
        )
        runs = LocalAgentRunManager(self.db)
        run = runs.create(
            self.parent,
            "claude",
            f"Relaunched seat {key}",
            agent_name=self.agents[key],
            child_session_id=fresh,
        )
        runs.start(run.id)
        return fresh

    def retire(self, stage: str) -> None:
        """Ends the stage's seat, so only an alternative stage can take the claim."""
        session = self.session(stage)
        self.db.execute(
            "UPDATE agent_runs SET status = 'completed' WHERE child_session_id = %s", (session,)
        )
        self.db.execute("UPDATE sessions SET status = 'deleted' WHERE id = %s", (session,))

    def add_outsider(self) -> str:
        """A team member whose definition holds no claim-flow stage."""
        session = self.seats.add("outsider-agent", ["validation"])
        self.db.execute(
            "UPDATE sessions SET parent_session_id = %s WHERE id = %s", (self.parent, session)
        )
        return session


def _alternate_review_flow() -> PipelineDefinition:
    """A developer's implement stage is reviewed by the code reviewer seat or, when that seat is
    not live, by an agent-only docs reviewer the lead spawns."""
    definition = _flow()
    stages = dict(definition.claim_flow)
    stages[IMPLEMENT] = stages[IMPLEMENT].model_copy(update={"next": [REVIEW, REVIEW_DOCS]})
    stages[REVIEW_DOCS] = ClaimStage(
        agent=DOC_REVIEWER, done_when=["independent-review"], next=VALIDATE, on_rejected=IMPLEMENT
    )
    return definition.model_copy(update={"claim_flow": stages})


def _team(db: HubDatabase, sample_project: dict[str, Any], closer_seat: str = "close") -> _Team:
    return _build_team(db, sample_project, _flow(closer_seat))


def _docs_team(db: HubDatabase, sample_project: dict[str, Any]) -> _Team:
    return _build_team(db, sample_project, _docs_flow())


def _build_team(
    db: HubDatabase, sample_project: dict[str, Any], definition: PipelineDefinition
) -> _Team:
    project = sample_project["id"]
    task = LocalTaskManager(db).create_task(project, "Probe", validation_criteria="Works.")
    executions = LocalPipelineExecutionManager(db, project)
    execution = executions.create_execution(
        definition.name, definition_json=definition.model_dump_json()
    )
    # Registered before the seats: AgentSessions numbers its sessions from a fixed base.
    parent = SessionManager(db).register(
        external_id=f"pipeline-{execution.id}",
        source="pipeline",
        machine_id=require_machine_id(),
        project_id=project,
    )
    seats = AgentSessions(db, project)
    agents = {_seat_key(stage): _agent_name(stage) for stage in definition.claim_flow.values()}
    sessions = {key: seats.add(agents[key], _seat_kinds(definition, key)) for key in sorted(agents)}
    binding = TeamBinding(execution.id, task.id, project, "single", "main", None, None)
    admit = executions.create_step_execution(execution.id, ADMISSION_STEP)
    executions.update_step_execution(
        admit.id,
        status=StepStatus.COMPLETED,
        output_json=json.dumps({"team": asdict(binding)}),
    )
    seat_keys = sorted({stage.seat for stage in definition.claim_flow.values() if stage.seat})
    for seat in seat_keys:
        step = executions.create_step_execution(execution.id, seat)
        executions.update_step_execution(
            step.id,
            status=StepStatus.COMPLETED,
            output_json=json.dumps({"child_session_id": sessions[seat]}),
        )
    for sid in sessions.values():
        db.execute("UPDATE sessions SET parent_session_id = %s WHERE id = %s", (parent.id, sid))
    runs = LocalAgentRunManager(db)
    for key, sid in sessions.items():
        run = runs.create(
            parent.id, "claude", f"Seat {key}", agent_name=agents[key], child_session_id=sid
        )
        runs.start(run.id)
    db.execute("UPDATE sessions SET status = 'deleted' WHERE id = %s", (parent.id,))
    LocalTaskManager(db).claim_task(task.id, sessions[_seat_key(definition.claim_flow[IMPLEMENT])])
    return _Team(db, definition, task.id, str(parent.id), seats, sessions, agents)


def test_re_candidate_after_explicit_hand_off_moves_to_checker(
    temp_db: HubDatabase, sample_project: dict[str, Any]
) -> None:
    team = _team(temp_db, sample_project)
    builder, checker = team.session(IMPLEMENT), team.session(REVIEW)
    team.chain(builder, IMPLEMENT, C1)
    assert team.holder() == checker

    team.hand_off(recipient=builder, holder=checker)
    team.chain(builder, IMPLEMENT, C2)

    assert team.holder() == checker


def test_superseding_test_before_then_re_recorded_chain_moves_to_checker(
    temp_db: HubDatabase, sample_project: dict[str, Any]
) -> None:
    team = _team(temp_db, sample_project)
    builder, checker = team.session(IMPLEMENT), team.session(REVIEW)
    first = team.chain(builder, IMPLEMENT, C1)
    assert team.holder() == checker

    team.hand_off(recipient=builder, holder=checker)
    team.attest(builder, "test-before", IMPLEMENT, supersedes_id=first[0])
    assert team.holder() == builder
    for kind in team.definition.claim_flow[IMPLEMENT].done_when[1:]:
        team.attest(builder, kind, IMPLEMENT, sha=C2)

    assert team.holder() == checker


def test_review_chain_moves_to_closer_after_explicit_hand_off_to_checker(
    temp_db: HubDatabase, sample_project: dict[str, Any]
) -> None:
    team = _team(temp_db, sample_project)
    builder, checker = team.session(IMPLEMENT), team.session(REVIEW)
    closer = team.session(VALIDATE)
    team.chain(builder, IMPLEMENT, C1)
    team.attest(checker, "independent-review", REVIEW, result="rejected", sha=C1)
    assert team.holder() == builder

    team.hand_off(recipient=checker, holder=builder)
    team.chain(builder, IMPLEMENT, C2)
    assert team.holder() == checker
    team.chain(checker, REVIEW, C2, first_reference=C2)

    assert team.holder() == closer


def test_incomplete_implement_chain_does_not_move(
    temp_db: HubDatabase, sample_project: dict[str, Any]
) -> None:
    team = _team(temp_db, sample_project)
    builder = team.session(IMPLEMENT)
    kinds = team.definition.claim_flow[IMPLEMENT].done_when
    for index, kind in enumerate(kinds[:-1]):
        team.attest(builder, kind, IMPLEMENT, sha=None if index == 0 else C1)

    assert team.holder() == builder


def test_superseded_evidence_alone_does_not_move(
    temp_db: HubDatabase, sample_project: dict[str, Any]
) -> None:
    team = _team(temp_db, sample_project)
    builder, checker = team.session(IMPLEMENT), team.session(REVIEW)
    first = team.chain(builder, IMPLEMENT, C1)
    team.attest(checker, "independent-review", REVIEW, result="rejected", sha=C1)
    assert team.holder() == builder

    team.attest(builder, "validation", IMPLEMENT, sha=C1, supersedes_id=first[-1])

    assert team.holder() == builder


def test_evidence_for_a_different_sha_refuses_and_does_not_move(
    temp_db: HubDatabase, sample_project: dict[str, Any]
) -> None:
    team = _team(temp_db, sample_project)
    builder = team.session(IMPLEMENT)
    kinds = team.definition.claim_flow[IMPLEMENT].done_when
    for index, kind in enumerate(kinds[:-1]):
        team.attest(builder, kind, IMPLEMENT, sha=None if index == 0 else C1)
    count = len(list_attestations(temp_db, team.task_id))

    with pytest.raises(ValueError, match="requires one reference"):
        team.attest(builder, kinds[-1], IMPLEMENT, sha=C2)

    assert team.holder() == builder
    assert len(list_attestations(temp_db, team.task_id)) == count


def test_rejection_returns_claim_and_corrected_chain_moves_to_checker(
    temp_db: HubDatabase, sample_project: dict[str, Any]
) -> None:
    team = _team(temp_db, sample_project)
    builder, checker = team.session(IMPLEMENT), team.session(REVIEW)
    team.chain(builder, IMPLEMENT, C1)
    team.attest(checker, "independent-review", REVIEW, result="rejected", sha=C1)
    assert team.holder() == builder

    team.chain(builder, IMPLEMENT, C2)

    assert team.holder() == checker


def test_closer_seat_that_also_builds_records_validation_without_refusal(
    temp_db: HubDatabase, sample_project: dict[str, Any]
) -> None:
    """One seat runs two stages: its validation after the claim returns must resolve the marker."""
    team = _team(temp_db, sample_project, closer_seat="build")
    builder, checker = team.session(IMPLEMENT), team.session(REVIEW)
    team.chain(builder, IMPLEMENT, C1)
    team.chain(checker, REVIEW, C1, first_reference=C1)
    assert team.holder() == builder

    team.attest(builder, "validation", VALIDATE, sha=C1)

    assert team.holder() == builder


def test_docs_writer_and_reviewer_move_the_claim_to_the_closer_without_a_hand_off(
    temp_db: HubDatabase, sample_project: dict[str, Any]
) -> None:
    team = _docs_team(temp_db, sample_project)
    writer, reviewer = team.session(IMPLEMENT), team.session(REVIEW)
    closer = team.session(VALIDATE)
    team.chain(writer, IMPLEMENT, C1)
    assert team.holder() == reviewer

    team.attest(reviewer, "independent-review", REVIEW, sha=C1)

    assert team.holder() == closer


def test_docs_reviewer_rejection_returns_the_claim_to_the_writer(
    temp_db: HubDatabase, sample_project: dict[str, Any]
) -> None:
    team = _docs_team(temp_db, sample_project)
    writer, reviewer = team.session(IMPLEMENT), team.session(REVIEW)
    team.chain(writer, IMPLEMENT, C1)

    team.attest(reviewer, "independent-review", REVIEW, result="rejected", sha=C1)

    assert team.holder() == writer


def test_correction_of_a_reviewed_candidate_after_a_return_moves_the_claim_to_validate(
    temp_db: HubDatabase, sample_project: dict[str, Any]
) -> None:
    """A developer re-records one kind on the candidate validate already passed (#23838 shape)."""
    team = _team(temp_db, sample_project)
    builder, checker = team.session(IMPLEMENT), team.session(REVIEW)
    closer = team.session(VALIDATE)
    first = team.chain(builder, IMPLEMENT, C1)
    team.chain(checker, REVIEW, C1, first_reference=C1)
    team.hand_off(recipient=builder, holder=closer)
    assert team.holder() == builder

    team.attest(builder, "test-before", IMPLEMENT, supersedes_id=first[0])

    assert team.holder() == closer


def test_fresh_candidate_after_a_return_is_reviewed_before_validate(
    temp_db: HubDatabase, sample_project: dict[str, Any]
) -> None:
    team = _team(temp_db, sample_project)
    builder, checker = team.session(IMPLEMENT), team.session(REVIEW)
    closer = team.session(VALIDATE)
    team.chain(builder, IMPLEMENT, C1)
    team.chain(checker, REVIEW, C1, first_reference=C1)
    team.hand_off(recipient=builder, holder=closer)

    team.chain(builder, IMPLEMENT, C2)

    assert team.holder() == checker


def test_rejected_correction_after_a_return_keeps_the_claim_with_the_developer(
    temp_db: HubDatabase, sample_project: dict[str, Any]
) -> None:
    team = _team(temp_db, sample_project)
    builder, checker = team.session(IMPLEMENT), team.session(REVIEW)
    closer = team.session(VALIDATE)
    chain = team.chain(builder, IMPLEMENT, C1)
    team.chain(checker, REVIEW, C1, first_reference=C1)
    team.hand_off(recipient=builder, holder=closer)
    self_review = chain[team.definition.claim_flow[IMPLEMENT].done_when.index("self-review")]

    team.attest(
        builder, "self-review", IMPLEMENT, result="rejected", sha=C1, supersedes_id=self_review
    )

    assert team.holder() == builder


def test_correction_on_another_candidate_does_not_move_a_reviewed_candidate(
    temp_db: HubDatabase, sample_project: dict[str, Any]
) -> None:
    team = _team(temp_db, sample_project)
    builder, checker = team.session(IMPLEMENT), team.session(REVIEW)
    closer = team.session(VALIDATE)
    first = team.chain(builder, IMPLEMENT, C1)
    team.chain(checker, REVIEW, C1, first_reference=C1)
    team.hand_off(recipient=builder, holder=closer)

    team.attest(builder, "test-before", IMPLEMENT, sha=C2, supersedes_id=first[0])

    assert team.holder() == builder


def test_developer_candidate_reviewed_by_docs_reviewer_moves_to_closer(
    temp_db: HubDatabase, sample_project: dict[str, Any]
) -> None:
    """The code reviewer seat is not live, so the developer's completed stage routes to the docs
    reviewer and its approval moves the claim to validate (#25802 lead repro)."""
    team = _build_team(temp_db, sample_project, _alternate_review_flow())
    team.retire(REVIEW)
    builder, reviewer = team.session(IMPLEMENT), team.session(REVIEW_DOCS)
    closer = team.session(VALIDATE)
    team.chain(builder, IMPLEMENT, C1)
    assert team.holder() == reviewer

    team.chain(reviewer, REVIEW_DOCS, C1)

    assert team.holder() == closer


def test_docs_reviewer_rejection_of_developer_candidate_returns_claim_to_developer(
    temp_db: HubDatabase, sample_project: dict[str, Any]
) -> None:
    team = _build_team(temp_db, sample_project, _alternate_review_flow())
    team.retire(REVIEW)
    builder, reviewer = team.session(IMPLEMENT), team.session(REVIEW_DOCS)
    team.chain(builder, IMPLEMENT, C1)

    team.attest(reviewer, "independent-review", REVIEW_DOCS, result="rejected", sha=C1)

    assert team.holder() == builder


def test_claim_stage_names_exactly_one_seat_or_agent() -> None:
    with pytest.raises(ValueError, match="exactly one of seat or agent"):
        PipelineDefinition(
            name="synthetic-claim-flow",
            steps=[
                PipelineStep(
                    id=ADMISSION_STEP, mcp=MCPStepConfig(server="synthetic", tool="admit_team")
                )
            ],
            claim_flow={IMPLEMENT: ClaimStage(done_when=["test-before"])},
        )


def test_rejection_by_checker_relaunched_after_it_took_the_claim_returns_it(
    temp_db: HubDatabase, sample_project: dict[str, Any]
) -> None:
    """A relaunch makes a newer live session for the checker seat; the rejecting holder still returns."""
    team = _team(temp_db, sample_project)
    builder, checker = team.session(IMPLEMENT), team.session(REVIEW)
    team.chain(builder, IMPLEMENT, C1)
    assert team.holder() == checker
    team.relaunch(REVIEW)
    assert team.holder() == checker

    team.attest(checker, "independent-review", REVIEW, result="rejected", sha=C1)

    assert team.holder() == builder


def test_evidence_from_a_session_outside_the_flow_is_refused(
    temp_db: HubDatabase, sample_project: dict[str, Any]
) -> None:
    team = _team(temp_db, sample_project)
    builder = team.session(IMPLEMENT)
    outsider = team.add_outsider()
    team.hand_off(recipient=outsider, holder=builder)
    count = len(list_attestations(temp_db, team.task_id))

    with pytest.raises(ValueError, match="has no claim flow stage"):
        team.attest(outsider, "validation", VALIDATE, sha=C1)

    assert team.holder() == outsider
    assert len(list_attestations(temp_db, team.task_id)) == count
