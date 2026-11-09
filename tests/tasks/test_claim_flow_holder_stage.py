"""Claim-flow holder regressions for #25795.

Each test runs a synthetic claim_flow declared inline: stage names, seats and done_when kinds
come from the test, never from an installed pipeline. A direct claim or an explicit hand-off
changes the claim holder without writing a claim-flow marker; the holder's complete evidence must
still move the claim, a relaunched seat must not strand it, and evidence from a session outside
the flow must be refused rather than ignored.
"""

import json
import logging
from dataclasses import asdict, dataclass
from typing import Any

import pytest

from gobby.agents.team_admission_records import TeamBinding
from gobby.mcp_proxy.tools.tasks import create_task_registry
from gobby.storage.agents import LocalAgentRunManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.pipelines import LocalPipelineExecutionManager
from gobby.storage.sessions import SessionManager
from gobby.storage.tasks import LocalTaskManager
from gobby.storage.team_records import ADMISSION_STEP
from gobby.tasks.attestations import (
    AttestationRecording,
    AttestationSubmission,
    list_attestations,
    record_attestation_recording,
)
from gobby.tasks.claim_flow import ClaimFlowStall
from gobby.utils.machine_id import require_machine_id
from gobby.utils.session_context import session_context_for_test
from gobby.workflows.pipeline_models import (
    ClaimStage,
    MCPStepConfig,
    PipelineDefinition,
    PipelineStep,
)
from gobby.workflows.pipeline_state import ExecutionStatus, StepStatus
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
IMPLEMENT_DOCS = "implement-docs"
DOCS = "docs"
CODE = "code"


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


def _recording(
    db: HubDatabase,
    task_id: str,
    session: str,
    kind: str,
    stage: str,
    *,
    result: str = "approved",
    sha: str | None = None,
    supersedes_id: str | None = None,
) -> AttestationRecording:
    return record_attestation_recording(
        db,
        task_id=task_id,
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


def _record(
    db: HubDatabase,
    task_id: str,
    session: str,
    kind: str,
    stage: str,
    *,
    result: str = "approved",
    sha: str | None = None,
    supersedes_id: str | None = None,
) -> str:
    return _recording(
        db,
        task_id,
        session,
        kind,
        stage,
        result=result,
        sha=sha,
        supersedes_id=supersedes_id,
    ).record.id


@dataclass
class _Team:
    db: HubDatabase
    definition: PipelineDefinition
    task_id: str
    parent: str
    seats: AgentSessions
    sessions: dict[str, str]
    agents: dict[str, str]
    execution_id: str

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
        return _record(
            self.db,
            self.task_id,
            session,
            kind,
            stage,
            result=result,
            sha=sha,
            supersedes_id=supersedes_id,
        )

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


def _lane_flow() -> PipelineDefinition:
    """A developer's implement stage takes docs-category tasks to the docs reviewer and every other
    task to the code reviewer. A docs writer's candidate takes the docs lane only."""
    definition = _flow()
    base = definition.claim_flow
    return definition.model_copy(
        update={
            "claim_flow": {
                IMPLEMENT: base[IMPLEMENT].model_copy(update={"next": [REVIEW_DOCS, REVIEW]}),
                REVIEW: base[REVIEW],
                VALIDATE: base[VALIDATE],
                IMPLEMENT_DOCS: ClaimStage(
                    agent=DOC_WRITER, done_when=base[IMPLEMENT].done_when, next=REVIEW_DOCS
                ),
                REVIEW_DOCS: ClaimStage(
                    agent=DOC_REVIEWER,
                    categories=[DOCS],
                    done_when=["independent-review"],
                    next=VALIDATE,
                    on_rejected=[IMPLEMENT_DOCS, IMPLEMENT],
                ),
            }
        }
    )


def _team(db: HubDatabase, sample_project: dict[str, Any], closer_seat: str = "close") -> _Team:
    return _build_team(db, sample_project, _flow(closer_seat))


def _docs_team(db: HubDatabase, sample_project: dict[str, Any]) -> _Team:
    return _build_team(db, sample_project, _docs_flow())


def _build_team(
    db: HubDatabase,
    sample_project: dict[str, Any],
    definition: PipelineDefinition,
    category: str | None = None,
    *,
    grants: dict[str, list[str]] | None = None,
) -> _Team:
    project = sample_project["id"]
    task = LocalTaskManager(db).create_task(
        project, "Probe", validation_criteria="Works.", category=category
    )
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
    granted = grants or {}
    sessions = {
        key: seats.add(agents[key], granted.get(key, _seat_kinds(definition, key)))
        for key in sorted(agents)
    }
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
    return _Team(db, definition, task.id, str(parent.id), seats, sessions, agents, execution.id)


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


def test_developer_docs_candidate_reaches_validate_when_the_docs_reviewer_approves(
    temp_db: HubDatabase, sample_project: dict[str, Any]
) -> None:
    """A developer implements a docs-category task; the docs reviewer's approval moves the claim to
    the closer with no code reviewer involved (#23583 shape, whoever implemented it)."""
    team = _build_team(temp_db, sample_project, _lane_flow(), category=DOCS)
    builder, reviewer = team.session(IMPLEMENT), team.session(REVIEW_DOCS)
    closer = team.session(VALIDATE)
    team.chain(builder, IMPLEMENT, C1)
    assert team.holder() == reviewer

    team.chain(reviewer, REVIEW_DOCS, C1)

    assert team.holder() == closer


def test_code_candidate_goes_to_the_code_reviewer_while_a_docs_reviewer_is_live(
    temp_db: HubDatabase, sample_project: dict[str, Any]
) -> None:
    """Routing follows the task category, never which reviewer seat happens to be live."""
    team = _build_team(temp_db, sample_project, _lane_flow(), category=CODE)
    builder, reviewer = team.session(IMPLEMENT), team.session(REVIEW)
    team.chain(builder, IMPLEMENT, C1)

    assert team.holder() == reviewer


def test_docs_reviewer_cannot_move_a_code_candidate_when_the_code_reviewer_is_absent(
    temp_db: HubDatabase, sample_project: dict[str, Any]
) -> None:
    """With the code reviewer gone, the code candidate is refused and a docs reviewer's approval of
    it does not move the claim to validate."""
    team = _build_team(temp_db, sample_project, _lane_flow(), category=CODE)
    team.retire(REVIEW)
    builder, docs_reviewer = team.session(IMPLEMENT), team.session(REVIEW_DOCS)
    with pytest.raises(ValueError, match="needs its live pipeline seat"):
        team.chain(builder, IMPLEMENT, C1)

    team.attest(docs_reviewer, "independent-review", REVIEW_DOCS, sha=C1)

    assert team.holder() == builder


def test_code_candidate_with_only_a_docs_route_is_refused(
    temp_db: HubDatabase, sample_project: dict[str, Any]
) -> None:
    definition = _lane_flow()
    stages = dict(definition.claim_flow)
    stages[IMPLEMENT] = stages[IMPLEMENT].model_copy(update={"next": [REVIEW_DOCS]})
    flow = definition.model_copy(update={"claim_flow": stages})
    team = _build_team(temp_db, sample_project, flow, category=CODE)
    builder = team.session(IMPLEMENT)

    with pytest.raises(ValueError, match="has no route for category"):
        team.chain(builder, IMPLEMENT, C1)


def test_docs_reviewer_rejection_of_a_developer_docs_candidate_returns_it_to_the_developer(
    temp_db: HubDatabase, sample_project: dict[str, Any]
) -> None:
    """The writer seat is live too, but the developer produced this candidate, so it returns there."""
    team = _build_team(temp_db, sample_project, _lane_flow(), category=DOCS)
    builder, reviewer = team.session(IMPLEMENT), team.session(REVIEW_DOCS)
    team.chain(builder, IMPLEMENT, C1)

    team.attest(reviewer, "independent-review", REVIEW_DOCS, result="rejected", sha=C1)

    assert team.holder() == builder


def test_docs_writer_rejection_returns_to_the_writer_beside_a_live_developer(
    temp_db: HubDatabase, sample_project: dict[str, Any]
) -> None:
    team = _build_team(temp_db, sample_project, _lane_flow(), category=DOCS)
    builder, writer = team.session(IMPLEMENT), team.session(IMPLEMENT_DOCS)
    reviewer = team.session(REVIEW_DOCS)
    team.hand_off(recipient=writer, holder=builder)
    team.chain(writer, IMPLEMENT_DOCS, C1)
    assert team.holder() == reviewer

    team.attest(reviewer, "independent-review", REVIEW_DOCS, result="rejected", sha=C1)

    assert team.holder() == writer


def test_rejection_of_a_candidate_no_lane_produced_refuses_the_evidence(
    temp_db: HubDatabase, sample_project: dict[str, Any]
) -> None:
    team = _build_team(temp_db, sample_project, _lane_flow(), category=DOCS)
    builder, reviewer = team.session(IMPLEMENT), team.session(REVIEW_DOCS)
    team.chain(builder, IMPLEMENT, C1)

    with pytest.raises(ValueError, match="cannot tell which lane produced"):
        team.attest(reviewer, "independent-review", REVIEW_DOCS, result="rejected", sha=C2)

    assert team.holder() == reviewer


def test_code_correction_docs_approval_does_not_skip_the_code_reviewer(
    temp_db: HubDatabase, sample_project: dict[str, Any]
) -> None:
    """A correction routes like forward advancement. For a code task the docs lane is no route,
    so a docs reviewer's approval of the corrected candidate cannot carry the claim to validate."""
    team = _build_team(temp_db, sample_project, _lane_flow(), category=CODE)
    builder, checker = team.session(IMPLEMENT), team.session(REVIEW)
    docs_reviewer, closer = team.session(REVIEW_DOCS), team.session(VALIDATE)
    first = team.chain(builder, IMPLEMENT, C1)
    team.chain(checker, REVIEW, C1, first_reference=C1)
    team.hand_off(recipient=builder, holder=closer)

    team.attest(docs_reviewer, "independent-review", REVIEW_DOCS, sha=C2)
    for index, kind in enumerate(team.definition.claim_flow[IMPLEMENT].done_when):
        team.attest(builder, kind, IMPLEMENT, sha=C2, supersedes_id=first[index])

    assert team.holder() == checker


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


@dataclass
class _Epic:
    """An epic team with leaves ``a`` and ``b``; each live seat is bound to one leaf by its run."""

    db: HubDatabase
    definition: PipelineDefinition
    epic_id: str
    leaves: dict[str, str]
    seats: dict[str, str]

    def seat(self, name: str) -> str:
        return self.seats[name]

    def attest(
        self,
        leaf: str,
        session: str,
        kind: str,
        stage: str,
        *,
        result: str = "approved",
        sha: str | None = None,
    ) -> str:
        return _record(self.db, self.leaves[leaf], session, kind, stage, result=result, sha=sha)

    def chain(self, leaf: str, session: str, stage: str, sha: str) -> None:
        for kind in self.definition.claim_flow[stage].done_when:
            self.attest(leaf, session, kind, stage, sha=sha)

    def holder(self, leaf: str) -> str:
        return str(LocalTaskManager(self.db).get_task(self.leaves[leaf]).claimed_by_session_id)


def _build_epic(
    db: HubDatabase,
    sample_project: dict[str, Any],
    seats: list[tuple[str, str, str]],
    claims: dict[str, str],
) -> _Epic:
    """Admits an epic team whose live seats are ``(name, pipeline seat, leaf)``.

    ``claims`` maps each leaf to the name of the seat that claims it.
    """
    project = sample_project["id"]
    tasks = LocalTaskManager(db)
    epic = tasks.create_task(project, "Epic", task_type="epic")
    leaves = {
        name: tasks.create_task(
            project, f"Leaf {name}", validation_criteria="Works.", parent_task_id=epic.id
        ).id
        for name in ("a", "b")
    }
    definition = _flow()
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
    fixtures = AgentSessions(db, project)
    runs = LocalAgentRunManager(db)
    sessions: dict[str, str] = {}
    for name, seat, leaf in seats:
        sid = fixtures.add(AGENTS[seat], _seat_kinds(definition, seat))
        sessions[name] = sid
        db.execute("UPDATE sessions SET parent_session_id = %s WHERE id = %s", (parent.id, sid))
        run = runs.create(
            parent.id,
            "claude",
            f"Seat {name}",
            agent_name=AGENTS[seat],
            child_session_id=sid,
            task_id=leaves[leaf],
        )
        runs.start(run.id)
    binding = TeamBinding(execution.id, epic.id, project, "single", "main", None, None, mode="epic")
    admit = executions.create_step_execution(execution.id, ADMISSION_STEP)
    executions.update_step_execution(
        admit.id,
        status=StepStatus.COMPLETED,
        output_json=json.dumps({"team": asdict(binding)}),
    )
    for seat in sorted({key for _, key, _ in seats}):
        first = next(name for name, key, _ in seats if key == seat)
        step = executions.create_step_execution(execution.id, seat)
        executions.update_step_execution(
            step.id,
            status=StepStatus.COMPLETED,
            output_json=json.dumps({"child_session_id": sessions[first]}),
        )
    db.execute("UPDATE sessions SET status = 'deleted' WHERE id = %s", (parent.id,))
    for leaf, name in claims.items():
        tasks.claim_task(leaves[leaf], sessions[name])
    return _Epic(db, definition, epic.id, leaves, sessions)


def test_two_live_developers_on_two_leaves_each_move_their_own_claim(
    temp_db: HubDatabase, sample_project: dict[str, Any]
) -> None:
    team = _build_epic(
        temp_db,
        sample_project,
        seats=[
            ("dev-a", "build", "a"),
            ("dev-b", "build", "b"),
            ("check-a", "check", "a"),
            ("check-b", "check", "b"),
        ],
        claims={"a": "dev-a", "b": "dev-b"},
    )

    team.chain("a", team.seat("dev-a"), IMPLEMENT, C1)
    team.chain("b", team.seat("dev-b"), IMPLEMENT, C2)

    assert team.holder("a") == team.seat("check-a")
    assert team.holder("b") == team.seat("check-b")


def test_rejection_on_one_leaf_returns_its_claim_to_its_own_developer(
    temp_db: HubDatabase, sample_project: dict[str, Any]
) -> None:
    team = _build_epic(
        temp_db,
        sample_project,
        seats=[
            ("dev-a", "build", "a"),
            ("dev-b", "build", "b"),
            ("check-a", "check", "a"),
            ("check-b", "check", "b"),
        ],
        claims={"a": "dev-a", "b": "dev-b"},
    )
    team.chain("a", team.seat("dev-a"), IMPLEMENT, C1)
    team.chain("b", team.seat("dev-b"), IMPLEMENT, C2)

    team.attest("a", team.seat("check-a"), "independent-review", REVIEW, result="rejected", sha=C1)

    assert team.holder("a") == team.seat("dev-a")
    assert team.holder("b") == team.seat("check-b")


def test_seat_bound_to_another_leaf_never_receives_the_claim(
    temp_db: HubDatabase, sample_project: dict[str, Any]
) -> None:
    team = _build_epic(
        temp_db,
        sample_project,
        seats=[("dev-a", "build", "a"), ("check-b", "check", "b")],
        claims={"a": "dev-a"},
    )
    builder = team.seat("dev-a")
    for kind in team.definition.claim_flow[IMPLEMENT].done_when[:-1]:
        team.attest("a", builder, kind, IMPLEMENT, sha=C1)

    with pytest.raises(ValueError, match="needs its live pipeline seat"):
        team.attest("a", builder, "validation", IMPLEMENT, sha=C1)

    assert team.holder("a") == builder


def test_two_live_seats_bound_to_one_leaf_refuse_and_name_the_task_and_both_sessions(
    temp_db: HubDatabase, sample_project: dict[str, Any]
) -> None:
    team = _build_epic(
        temp_db,
        sample_project,
        seats=[("dev-a", "build", "a"), ("dev-a2", "build", "a")],
        claims={"a": "dev-a"},
    )

    with pytest.raises(ValueError, match="ambiguous live pipeline seats") as raised:
        team.attest("a", team.seat("dev-a"), "test-before", IMPLEMENT, sha=C1)

    message = str(raised.value)
    assert team.leaves["a"] in message
    assert team.seat("dev-a") in message
    assert team.seat("dev-a2") in message
    assert team.holder("a") == team.seat("dev-a")


def test_idle_seat_superseded_by_a_newer_live_run_resolves_to_the_newer(
    temp_db: HubDatabase, sample_project: dict[str, Any]
) -> None:
    team = _build_epic(
        temp_db,
        sample_project,
        seats=[("dev-a", "build", "a"), ("dev-a2", "build", "a")],
        claims={"a": "dev-a2"},
    )
    temp_db.execute("UPDATE sessions SET status = 'idle' WHERE id = %s", (team.seat("dev-a"),))
    temp_db.execute(
        "UPDATE agent_runs SET created_at = created_at - INTERVAL '1 minute' "
        "WHERE child_session_id = %s",
        (team.seat("dev-a"),),
    )

    team.attest("a", team.seat("dev-a2"), "test-before", IMPLEMENT, sha=C1)

    assert team.holder("a") == team.seat("dev-a2")
    recorded = list_attestations(temp_db, team.leaves["a"])
    assert [(item.kind, item.session_id) for item in recorded] == [
        ("test-before", team.seat("dev-a2"))
    ]


def test_idle_claim_holder_is_never_dropped_while_a_live_sibling_is_bound(
    temp_db: HubDatabase, sample_project: dict[str, Any]
) -> None:
    team = _build_epic(
        temp_db,
        sample_project,
        seats=[("dev-a", "build", "a"), ("dev-a2", "build", "a")],
        claims={"a": "dev-a"},
    )
    temp_db.execute("UPDATE sessions SET status = 'idle' WHERE id = %s", (team.seat("dev-a"),))

    with pytest.raises(ValueError, match="ambiguous live pipeline seats"):
        team.attest("a", team.seat("dev-a"), "test-before", IMPLEMENT, sha=C1)

    assert team.holder("a") == team.seat("dev-a")
    assert list_attestations(temp_db, team.leaves["a"]) == []


def test_newer_idle_seat_is_not_superseded_and_keeps_the_stage_ambiguous(
    temp_db: HubDatabase, sample_project: dict[str, Any]
) -> None:
    team = _build_epic(
        temp_db,
        sample_project,
        seats=[("dev-a", "build", "a"), ("dev-a2", "build", "a")],
        claims={"a": "dev-a"},
    )
    temp_db.execute("UPDATE sessions SET status = 'idle' WHERE id = %s", (team.seat("dev-a2"),))

    with pytest.raises(ValueError, match="ambiguous live pipeline seats"):
        team.attest("a", team.seat("dev-a"), "test-before", IMPLEMENT, sha=C1)

    assert list_attestations(temp_db, team.leaves["a"]) == []


def test_single_idle_seat_on_the_leaf_still_serves_the_stage(
    temp_db: HubDatabase, sample_project: dict[str, Any]
) -> None:
    team = _build_epic(
        temp_db,
        sample_project,
        seats=[("dev-a", "build", "a")],
        claims={"a": "dev-a"},
    )
    temp_db.execute("UPDATE sessions SET status = 'idle' WHERE id = %s", (team.seat("dev-a"),))

    team.attest("a", team.seat("dev-a"), "test-before", IMPLEMENT, sha=C1)

    assert [item.kind for item in list_attestations(temp_db, team.leaves["a"])] == ["test-before"]


def _stall_flow() -> PipelineDefinition:
    """The implement, check and validate flow at a pinned version, so a stall must name it."""
    return _flow().model_copy(update={"version": "2.3.0"})


def _stall_warnings(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [
        record.getMessage()
        for record in caplog.records
        if record.name == "gobby.tasks.claim_flow" and record.levelno == logging.WARNING
    ]


def test_done_when_kind_the_seat_cannot_write_stalls_the_stage_and_keeps_the_claim(
    temp_db: HubDatabase, sample_project: dict[str, Any], caplog: pytest.LogCaptureFixture
) -> None:
    team = _build_team(
        temp_db,
        sample_project,
        _stall_flow(),
        grants={"build": ["test-before", "implementation", "test-after"]},
    )
    builder = team.session(IMPLEMENT)

    with caplog.at_level(logging.WARNING, logger="gobby.tasks.claim_flow"):
        recordings = [
            _recording(temp_db, team.task_id, builder, kind, IMPLEMENT, sha=C1)
            for kind in ("test-before", "implementation", "test-after")
        ]

    assert all(recording.created for recording in recordings)
    recorded = {record.id for record in list_attestations(temp_db, team.task_id)}
    assert recordings[-1].record.id in recorded
    assert team.holder() == builder
    assert recordings[-1].claim_flow_stalled == ClaimFlowStall(
        execution_id=team.execution_id,
        definition_version="2.3.0",
        stage=IMPLEMENT,
        unmet_kinds=["self-review", "validation"],
    )
    assert any(
        team.execution_id in message
        and "2.3.0" in message
        and IMPLEMENT in message
        and "self-review, validation" in message
        for message in _stall_warnings(caplog)
    )


def test_ended_execution_stalls_the_stage_and_keeps_the_claim(
    temp_db: HubDatabase, sample_project: dict[str, Any], caplog: pytest.LogCaptureFixture
) -> None:
    team = _build_team(temp_db, sample_project, _stall_flow())
    builder = team.session(IMPLEMENT)
    LocalPipelineExecutionManager(temp_db, sample_project["id"]).update_execution_status(
        team.execution_id, ExecutionStatus.COMPLETED
    )

    with caplog.at_level(logging.WARNING, logger="gobby.tasks.claim_flow"):
        recording = _recording(temp_db, team.task_id, builder, "test-before", IMPLEMENT, sha=C1)

    assert recording.created
    recorded = {record.id for record in list_attestations(temp_db, team.task_id)}
    assert recording.record.id in recorded
    assert team.holder() == builder
    assert recording.claim_flow_stalled == ClaimFlowStall(
        execution_id=team.execution_id,
        definition_version="2.3.0",
        stage=IMPLEMENT,
        unmet_kinds=["implementation", "test-after", "self-review", "validation"],
    )
    assert any(
        team.execution_id in message and "2.3.0" in message and IMPLEMENT in message
        for message in _stall_warnings(caplog)
    )


def test_grantable_stage_in_progress_reports_no_stall(
    temp_db: HubDatabase, sample_project: dict[str, Any], caplog: pytest.LogCaptureFixture
) -> None:
    team = _build_team(temp_db, sample_project, _stall_flow())
    builder = team.session(IMPLEMENT)

    with caplog.at_level(logging.WARNING, logger="gobby.tasks.claim_flow"):
        recording = _recording(temp_db, team.task_id, builder, "test-before", IMPLEMENT, sha=C1)

    assert recording.created
    assert recording.claim_flow_stalled is None
    assert team.holder() == builder
    assert _stall_warnings(caplog) == []


async def test_registry_record_reports_the_stall_beside_the_recorded_attestation(
    temp_db: HubDatabase, sample_project: dict[str, Any]
) -> None:
    team = _build_team(
        temp_db,
        sample_project,
        _stall_flow(),
        grants={"build": ["test-before", "implementation", "test-after"]},
    )
    builder = team.session(IMPLEMENT)
    _record(temp_db, team.task_id, builder, "test-before", IMPLEMENT, sha=C1)
    _record(temp_db, team.task_id, builder, "implementation", IMPLEMENT, sha=C1)

    with session_context_for_test(builder):
        result = await create_task_registry(LocalTaskManager(temp_db)).call(
            "record_attestation",
            {
                "task_id": team.task_id,
                "kind": "test-after",
                "result": "approved",
                "summary": "probe test-after approved",
                "reference": {"scheme": "git", "value": C1},
                "evidence": {
                    "stage": IMPLEMENT,
                    "criteria": [{"criterion": "Works.", "met": True, "how": "probe"}],
                },
            },
        )

    assert "error" not in result, result
    assert result["created"] is True
    assert result.get("claim_flow_stalled") == {
        "execution_id": team.execution_id,
        "definition_version": "2.3.0",
        "stage": IMPLEMENT,
        "unmet_kinds": ["self-review", "validation"],
    }
