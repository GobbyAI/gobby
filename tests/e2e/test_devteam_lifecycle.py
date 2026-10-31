"""One DevTeam task from its test commit to a cleaned tree, through the real gates (plan 6.1)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from gobby.agents.team_admission_records import SeatTarget, TeamAdmissionRefused
from gobby.agents.team_lifecycle import check_team_spawn
from gobby.agents.team_teardown import TEARDOWN_GRACE
from gobby.mcp_proxy.tools import agent_cancellation
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.inter_session_messages import InterSessionMessageManager
from gobby.storage.tasks import LocalTaskManager, TaskStepRecordsMissingError
from gobby.storage.worktrees import LocalWorktreeManager
from gobby.tasks.step_records import (
    StepRecordError,
    StepSubmission,
    TaskStep,
    list_task_step_records,
    record_task_step,
)
from gobby.workflows.engine.claim_handoff import hand_off_claim
from tests.fixtures.landing_case import make_landing_case
from tests.fixtures.review_approvals import grant_review_writes, grant_step_writes
from tests.fixtures.team_case import TeamWorld, git_in, make_team_world
from tests.fixtures.team_teardown_case import (
    Terminations,
    admitted_team,
    branch_exists,
    closed_at,
    daemon,
    session_status,
    teardown,
    teardown_pass,
)

pytestmark = pytest.mark.e2e

# Every code task needs approved test, implementation, review and closing records.
CLOSE_POLICY = {
    "enforce_step_records": True,
    "test_categories": ["code"],
    "review_categories": ["code"],
}


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


def commit(db: HubDatabase, task_id: str, tree: Path, name: str) -> str:
    """Commit ``name`` in the team tree and link it to the task, as a seat does."""
    path = tree / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"# {name}\n")
    git_in(tree, "add", name)
    git_in(tree, "commit", "-m", f"Add {name}")
    sha = git_in(tree, "rev-parse", "HEAD")
    LocalTaskManager(db).link_commit(task_id, sha)
    return sha


def approve(db: HubDatabase, session_id: str, task_id: str, step: TaskStep, sha: str) -> None:
    """Record ``session_id``'s approval of ``step`` for ``sha`` through the role-owned writer."""
    task = LocalTaskManager(db).get_task(task_id)
    record_task_step(
        db,
        task_id=task_id,
        session_id=session_id,
        step=step,
        submission=StepSubmission(
            status="approved",
            epoch=task.review_epoch,
            round_number=task.review_fail_count + 1,
            feedback=f"{step} approved for {sha[:12]}",
            commit_sha=sha,
        ),
    )


async def test_complete_single_task_team(world: TeamWorld, monkeypatch: pytest.MonkeyPatch) -> None:
    case = world.case
    db, project_id = case.db, case.project_id
    tasks = LocalTaskManager(db)
    terminations = Terminations(db)
    monkeypatch.setattr(agent_cancellation, "terminate_agent_run", terminations)
    project_json = {"id": project_id, "task_close_policy": CLOSE_POLICY}
    case.direct({".gobby/project.json": json.dumps(project_json) + "\n"})
    team = await admitted_team(world, "Ship one task through its team")
    manager, writer, developer, reviewer = team.seats
    task_id = team.task.id
    tasks.update_task(task_id, category="code")
    assert team.binding.worktree_path is not None and team.binding.worktree_id is not None
    tree = Path(team.binding.worktree_path)
    grant_step_writes(db, writer, project_id, "fixture-test-writer", ("test",))
    # A developer whose definition could also write reviews still cannot review its own task.
    grant_step_writes(db, developer, project_id, "fixture-developer", ("implementation", "review"))
    grant_review_writes(db, reviewer, project_id)

    # The Test Writer holds the claim and records the committed tests.
    tasks.claim_task(task_id, writer, force=True)
    tests_sha = commit(db, task_id, tree, "tests/test_ship.py")
    approve(db, writer, task_id, "test", tests_sha)

    # The Test Writer's candidate moves the claim to the reviewer, and the reviewer's LAND on the
    # tests moves it to the Developer. Each seat sends its message; no seat calls a hand-off tool.
    send = ("gobby-agents", "send_message")
    messages = InterSessionMessageManager(db)
    seq = team.task.seq_num
    candidate_line = f"EVENT=CANDIDATE TASK=#{seq} RUN=run WT=tree COMMIT={tests_sha} NOTE=red"
    candidate = messages.create_message(writer, reviewer, candidate_line)
    to_reviewer = hand_off_claim(db, writer, "review", send, {"message_ids": [candidate.id]})
    assert to_reviewer["handed_to"] == reviewer, to_reviewer
    approve(db, reviewer, task_id, "review", tests_sha)
    verdict = f"EVENT=CANDIDATE_VERDICT TASK=#{seq} SHA={tests_sha} VERDICT=LAND NOTE=ok"
    verdict_ids = [
        messages.create_message(reviewer, seat, verdict).id for seat in (developer, manager)
    ]
    to_developer = hand_off_claim(db, reviewer, "next_open", send, {"message_ids": verdict_ids})
    assert to_developer["handed_to"] == developer, to_developer
    assert tasks.get_task(task_id).claimed_by_session_id == developer
    sha = commit(db, task_id, tree, "docs/ship.md")
    approve(db, developer, task_id, "implementation", sha)

    # Review is independent: the implementer is refused, and neither it nor the reviewer lands.
    with pytest.raises(StepRecordError, match="cannot review its own task"):
        approve(db, developer, task_id, "review", sha)
    approve(db, reviewer, task_id, "review", sha)
    for lander, blocker in (
        (developer, "caller_is_maker"),
        (reviewer, "caller_not_task_manager"),
    ):
        refused = await case.land(tasks.get_task(task_id), sha, caller=lander)
        assert refused["landed"] is False and blocker in refused["blockers"], refused
    assert case.git("rev-parse", "trunk") != sha

    # The Dev Manager lands the reviewed commit, which marks the team tree merged.
    landed = await case.land(tasks.get_task(task_id), sha, caller=manager)
    assert (landed["landed"], landed["worktree_merged"]) == (True, True), landed
    assert case.git("rev-parse", "trunk") == sha

    # Close needs the manager's closing record too.
    with pytest.raises(TaskStepRecordsMissingError) as missing:
        tasks.close_task(
            task_id, reason="done", closed_in_session_id=manager, closed_commit_sha=sha
        )
    assert missing.value.missing_steps == ("closing",)
    approve(db, manager, task_id, "closing", sha)
    tasks.close_task(task_id, reason="done", closed_in_session_id=manager, closed_commit_sha=sha)
    closed = tasks.get_task(task_id)
    records = list_task_step_records(db, task_id)
    # The reviewer reviews the test SHA before implementation, then the implementation SHA.
    assert [(record.step, record.session_id) for record in records] == [
        ("test", writer),
        ("review", reviewer),
        ("implementation", developer),
        ("review", reviewer),
        ("closing", manager),
    ]

    # The closing manager receives its close response before any seat ends.
    runner = daemon(db)
    await teardown_pass(db, runner, closed_at(closed))
    assert terminations.calls == []
    await teardown_pass(db, runner, closed_at(closed) + TEARDOWN_GRACE)

    assert terminations.ended() == sorted(team.seats)
    assert {session_status(db, session) for session in team.seats} == {"expired"}
    assert teardown(world, team) == {"outcome": "cleaned"}
    assert not tree.exists()
    assert team.binding.worktree_id is not None
    assert LocalWorktreeManager(db).get(team.binding.worktree_id) is None
    assert not branch_exists(world, team.binding.branch)
    after = tasks.get_task(task_id)
    assert (after.closed_at, after.closed_commit_sha, after.commits) == (
        closed.closed_at,
        sha,
        [tests_sha, sha],
    )
    with pytest.raises(TeamAdmissionRefused) as closed_team:
        check_team_spawn(db, team.runbook, SeatTarget(project_path=team.binding.worktree_path))
    assert closed_team.value.code == "team_closed"
