"""The task claim moves along the review chain when a seat's own hand-off event is sent.

The engine hop runs on the sender's successful send. It moves the claim only while the sender
still holds it, to a live seat of the team whose definition writes the next step, and it never
hands a claim to a session that authored the work under review.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import pytest
from gobby.workflows.engine.claim_handoff import hand_off_claim

from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.inter_session_messages import InterSessionMessageManager
from gobby.storage.tasks import LocalTaskManager, Task
from gobby.storage.tasks._lifecycle_events import TaskLifecycleEventManager
from gobby.tasks.step_records import StepSubmission, TaskStep, record_task_step
from gobby.workflows.engine.seat_messages import candidate_event
from gobby.workflows.engine.seat_transitions import _candidate_rejection
from tests.fixtures.landing_case import LandingCase, make_landing_case
from tests.fixtures.review_approvals import grant_review_writes, grant_step_writes
from tests.fixtures.team_case import TeamWorld, make_team_world
from tests.fixtures.team_teardown_case import Team, admitted_team

pytestmark = pytest.mark.unit

SEND = ("gobby-agents", "send_message")


@pytest.fixture
def world(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> TeamWorld:
    return make_team_world(
        make_landing_case(temp_db, sample_project, tmp_path), tmp_path, monkeypatch
    )


@dataclass
class Chain:
    case: LandingCase
    team: Team
    task: Task
    manager: str
    writer: str
    developer: str
    reviewer: str
    tests_sha: str
    impl_sha: str

    @property
    def db(self) -> HubDatabase:
        return self.case.db


async def _chain(world: TeamWorld, title: str) -> Chain:
    """An admitted team whose Test Writer holds the claim and whose seats write their steps."""
    case = world.case
    db, project_id = case.db, case.project_id
    team = await admitted_team(world, title)
    manager, writer, developer, reviewer = team.seats
    grant_step_writes(db, manager, project_id, "fixture-dev-manager", ("closing",))
    grant_step_writes(db, writer, project_id, "fixture-test-writer", ("test",))
    grant_step_writes(db, developer, project_id, "fixture-developer", ("implementation",))
    grant_review_writes(db, reviewer, project_id)
    tests_sha = case.candidate("lane-tests", {"tests/test_chain.py": "RED"})
    impl_sha = case.candidate("lane-impl", {"docs/chain.md": "GREEN"}, base=tests_sha)
    tasks = LocalTaskManager(db)
    tasks.link_commit(team.task.id, tests_sha)
    tasks.link_commit(team.task.id, impl_sha)
    tasks.claim_task(team.task.id, writer, force=True)
    return Chain(
        case=case,
        team=team,
        task=tasks.get_task(team.task.id),
        manager=manager,
        writer=writer,
        developer=developer,
        reviewer=reviewer,
        tests_sha=tests_sha,
        impl_sha=impl_sha,
    )


def _record(
    chain: Chain,
    session: str,
    step: TaskStep,
    sha: str,
    *,
    status: Literal["approved", "rejected"] = "approved",
    round_number: int = 1,
) -> None:
    task = LocalTaskManager(chain.db).get_task(chain.task.id)
    record_task_step(
        chain.db,
        task_id=task.id,
        session_id=session,
        step=step,
        submission=StepSubmission(
            status=status,
            epoch=task.review_epoch,
            round_number=round_number,
            feedback=f"{step} {status} for {sha[:12]}",
            commit_sha=sha,
        ),
    )


def _send(chain: Chain, sender: str, content: str, *recipients: str) -> list[str]:
    messages = InterSessionMessageManager(chain.db)
    return [messages.create_message(sender, recipient, content).id for recipient in recipients]


def _hand(chain: Chain, sender: str, hop: str, ids: list[str]) -> dict[str, Any]:
    return hand_off_claim(chain.db, sender, hop, SEND, {"message_ids": ids})


def _candidate(chain: Chain, sha: str) -> str:
    return f"EVENT=CANDIDATE TASK=#{chain.task.seq_num} RUN=run WT=tree COMMIT={sha} NOTE=red"


def _verdict(chain: Chain, sha: str, verdict: str) -> str:
    return f"EVENT=CANDIDATE_VERDICT TASK=#{chain.task.seq_num} SHA={sha} VERDICT={verdict} NOTE=ok"


def _owner(chain: Chain) -> str | None:
    return LocalTaskManager(chain.db).get_task(chain.task.id).claimed_by_session_id


def _events(chain: Chain, to_state: str) -> list[Any]:
    return [
        event
        for event in TaskLifecycleEventManager(chain.db).list_lifecycle_events(chain.task.id)
        if event.to_state == to_state
    ]


@pytest.mark.parametrize("step", ["test", "implementation"])
async def test_candidate_hands_the_claim_to_the_reviewer_once(world: TeamWorld, step: str) -> None:
    chain = await _chain(world, f"Candidate hands off from {step}")
    if step == "test":
        sender, sha = chain.writer, chain.tests_sha
    else:
        sender, sha = chain.developer, chain.impl_sha
        LocalTaskManager(chain.db).claim_task(chain.task.id, sender, force=True)
    # The sender relays one CANDIDATE to its Dev Manager and to the reviewer: two messages.
    ids = _send(chain, sender, _candidate(chain, sha), chain.manager, chain.reviewer)

    first = _hand(chain, sender, "review", ids)
    replay = _hand(chain, sender, "review", ids)

    assert first["handed_to"] == chain.reviewer
    assert replay["handed_to"] is None
    assert _owner(chain) == chain.reviewer
    assert len(_events(chain, "claim:handed_off")) == 1


async def test_reviewer_land_on_test_candidate_hands_the_claim_to_the_developer(
    world: TeamWorld,
) -> None:
    chain = await _chain(world, "Test LAND opens implementation")
    _record(chain, chain.writer, "test", chain.tests_sha)
    LocalTaskManager(chain.db).claim_task(chain.task.id, chain.reviewer, force=True)
    _record(chain, chain.reviewer, "review", chain.tests_sha)
    ids = _send(
        chain,
        chain.reviewer,
        _verdict(chain, chain.tests_sha, "LAND"),
        chain.developer,
        chain.manager,
    )

    result = _hand(chain, chain.reviewer, "next_open", ids)

    assert result["handed_to"] == chain.developer
    assert _owner(chain) == chain.developer


async def test_reviewer_land_on_implementation_hands_the_claim_to_the_dev_manager(
    world: TeamWorld,
) -> None:
    chain = await _chain(world, "Implementation LAND closes")
    _record(chain, chain.writer, "test", chain.tests_sha)
    LocalTaskManager(chain.db).claim_task(chain.task.id, chain.developer, force=True)
    _record(chain, chain.developer, "implementation", chain.impl_sha)
    LocalTaskManager(chain.db).claim_task(chain.task.id, chain.reviewer, force=True)
    _record(chain, chain.reviewer, "review", chain.impl_sha)
    ids = _send(
        chain,
        chain.reviewer,
        _verdict(chain, chain.impl_sha, "LAND"),
        chain.manager,
        chain.developer,
    )

    result = _hand(chain, chain.reviewer, "next_open", ids)

    assert result["handed_to"] == chain.manager
    assert _owner(chain) == chain.manager


@pytest.mark.parametrize(("maker", "step"), [("writer", "test"), ("developer", "implementation")])
async def test_bounce_returns_the_claim_to_the_maker(
    world: TeamWorld, maker: str, step: TaskStep
) -> None:
    chain = await _chain(world, f"BOUNCE returns {step} to its maker")
    sha = chain.tests_sha if step == "test" else chain.impl_sha
    author = {"writer": chain.writer, "developer": chain.developer}[maker]
    _record(chain, author, step, sha)
    LocalTaskManager(chain.db).claim_task(chain.task.id, chain.reviewer, force=True)
    _record(chain, chain.reviewer, "review", sha, status="rejected", round_number=1)
    ids = _send(chain, chain.reviewer, _verdict(chain, sha, "BOUNCE"), author, chain.manager)

    result = _hand(chain, chain.reviewer, "maker", ids)

    assert result["handed_to"] == author
    assert _owner(chain) == author


async def test_second_bounce_keeps_the_claim_with_the_reviewer_who_fixes(
    world: TeamWorld,
) -> None:
    chain = await _chain(world, "Second BOUNCE is the fixer round")
    _record(chain, chain.writer, "test", chain.tests_sha)
    LocalTaskManager(chain.db).claim_task(chain.task.id, chain.developer, force=True)
    _record(chain, chain.developer, "implementation", chain.impl_sha)
    LocalTaskManager(chain.db).claim_task(chain.task.id, chain.reviewer, force=True)
    _record(chain, chain.reviewer, "review", chain.impl_sha, status="rejected", round_number=1)
    _record(chain, chain.reviewer, "review", chain.impl_sha, status="rejected", round_number=2)
    ids = _send(
        chain,
        chain.reviewer,
        _verdict(chain, chain.impl_sha, "BOUNCE"),
        chain.developer,
        chain.manager,
    )

    result = _hand(chain, chain.reviewer, "maker", ids)

    assert result["handed_to"] is None
    assert _owner(chain) == chain.reviewer


async def test_no_live_seat_records_a_pending_hand_off(world: TeamWorld) -> None:
    chain = await _chain(world, "No live reviewer")
    LocalTaskManager(chain.db).claim_task(chain.task.id, chain.developer, force=True)
    chain.db.execute("UPDATE sessions SET status = 'expired' WHERE id = %s", (chain.reviewer,))
    ids = _send(chain, chain.developer, _candidate(chain, chain.impl_sha), chain.reviewer)

    result = _hand(chain, chain.developer, "review", ids)

    assert result["handed_to"] is None
    assert _owner(chain) == chain.developer
    assert len(_events(chain, "claim:handoff_pending")) == 1


async def test_a_maker_of_the_work_is_never_handed_review(world: TeamWorld) -> None:
    chain = await _chain(world, "Maker never reviews")
    # The reviewer authored an earlier implementation, so it is a maker of this task.
    grant_step_writes(
        chain.db,
        chain.reviewer,
        chain.case.project_id,
        "fixture-fixer-reviewer",
        ("review", "implementation"),
    )
    _record(chain, chain.reviewer, "implementation", chain.tests_sha)
    LocalTaskManager(chain.db).claim_task(chain.task.id, chain.developer, force=True)
    ids = _send(chain, chain.developer, _candidate(chain, chain.impl_sha), chain.reviewer)

    result = _hand(chain, chain.developer, "review", ids)

    assert result["handed_to"] is None
    assert _owner(chain) == chain.developer
    assert len(_events(chain, "claim:handoff_pending")) == 1


async def test_reviewer_reads_the_candidate_after_the_hop(world: TeamWorld) -> None:
    chain = await _chain(world, "Binding follows the claim at send time")
    LocalTaskManager(chain.db).claim_task(chain.task.id, chain.developer, force=True)
    ids = _send(chain, chain.developer, _candidate(chain, chain.impl_sha), chain.reviewer)
    _hand(chain, chain.developer, "review", ids)
    message = InterSessionMessageManager(chain.db).get_message(ids[0])
    assert message is not None
    event = candidate_event(chain.db, message)
    assert event is not None

    rejection = _candidate_rejection(chain.db, {}, event, chain.reviewer)

    assert rejection is None
