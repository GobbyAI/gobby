"""A small DevTeam cohort keeps working through real release batches (plan 6.1).

One batch succeeds and one fails a check. While each batch holds the window, new teams and
landings wait and an admitted team keeps working; release and expiry reopen both.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from gobby.agents.team_lifecycle import TeamAdmissionRefused
from gobby.releases.batch import BatchRecord, latest_baseline, run_batch
from gobby.releases.store import HubReleaseLease
from gobby.releases.window import LEASE_TTL, acquire_window, read_window
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.session_models import Session
from gobby.storage.tasks import LocalTaskManager, Task
from gobby.tasks.land_commit import LandingResult
from tests.fixtures.landing_case import LandingCase, make_landing_case
from tests.fixtures.release_batch_case import Clock, Fakes
from tests.fixtures.team_case import TeamWorld, git_in, make_team_world
from tests.fixtures.team_teardown_case import Team, admitted_team, seated_team

pytestmark = pytest.mark.e2e

# What a held window shows: the new team and the landing wait, the admitted team works.
HELD = {
    "new_team": "release_window_active",
    "landing": (False, True),
    "rejoined": True,
    "spawned": True,
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


def reviewed_commit(case: LandingCase, team: Team, name: str) -> str:
    """Commit ``name`` in the team tree, link it and record its approved review."""
    tree = Path(team.binding.worktree_path)
    path = tree / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{path.stem.upper()} = 1\n")
    git_in(tree, "add", name)
    git_in(tree, "commit", "-m", f"Add {name}")
    sha = git_in(tree, "rev-parse", "HEAD")
    case.link(team.task, sha)
    case.approve(team.task, sha)
    return sha


async def land(case: LandingCase, team: Team, sha: str) -> LandingResult:
    """The team's Dev Manager lands its reviewed commit."""
    task = LocalTaskManager(case.db).get_task(team.task.id)
    return await case.land(task, sha, caller=team.seats[0])


def close(case: LandingCase, team: Team, sha: str) -> Task:
    tasks = LocalTaskManager(case.db)
    tasks.close_task(
        team.task.id, reason="done", closed_in_session_id=team.seats[0], closed_commit_sha=sha
    )
    return tasks.get_task(team.task.id)


async def held(
    world: TeamWorld, newcomer: Session, new_task: Task, team: Team, sha: str
) -> dict[str, object]:
    """Probe new admission, ``team``'s landing and ``team``'s own work inside the window."""
    observed: dict[str, object] = {}
    try:
        await world.admit(newcomer, new_task)
    except TeamAdmissionRefused as refused:
        observed["new_team"] = refused.code
    landing = await land(world.case, team, sha)
    blockers = landing.get("blockers", [])
    observed["landing"] = (landing["landed"], "release_window_active" in blockers)
    replacement = world.child(team.task)
    rejoined = await world.admit(replacement, team.task, team_id=team.binding.team_id)
    observed["rejoined"] = rejoined == team.binding
    observed["spawned"] = (
        await world.spawn(replacement, worktree_id=team.binding.worktree_id)
    ).get("success")
    world.finish(replacement)
    return observed


async def batch(
    world: TeamWorld,
    owner: str,
    clock: Clock,
    probe: tuple[Session, Task, Team, str],
    *,
    fail: str | None = None,
    base_sha: str | None = None,
) -> tuple[BatchRecord, Fakes, str]:
    """Acquire the window and run one batch; ``probe`` runs while its first check holds it."""
    case = world.case
    lease = await acquire_window(
        case.db, project_id=case.project_id, owner_session_id=owner, now=clock.now
    )
    fakes = Fakes(
        case, clock, fail=fail, loop=asyncio.get_running_loop(), during=lambda: held(world, *probe)
    )
    store = HubReleaseLease(
        case.db,
        project_id=case.project_id,
        owner_session_id=owner,
        token=lease.token,
        generation=lease.generation,
        clock=clock,
    )
    record = await run_batch(
        store,
        base_sha=base_sha,
        env=fakes.env(),
    )
    return record, fakes, lease.candidate_sha


async def test_concurrent_release_cohort(world: TeamWorld) -> None:
    case = world.case
    common = case.repo / ".git"
    tasks = LocalTaskManager(case.db)
    owner = case.session("orchestrator")
    # Admission and landing read the wall clock, so the lease does too.
    clock = Clock(datetime.now(UTC))

    # Team A lands and closes; team B holds a reviewed candidate it has not landed.
    first = await admitted_team(world, "Cohort team A lands first")
    first_sha = reviewed_commit(case, first, "src/gobby/cohort_a.py")
    assert (await land(case, first, first_sha))["landed"] is True
    closed_first = close(case, first, first_sha)
    second = await admitted_team(world, "Cohort team B lands after the release")
    second_sha = reviewed_commit(case, second, "src/gobby/cohort_b.py")
    third_task = case.task("Cohort team C starts after the release")
    third_runbook = world.child(third_task)

    # The first batch succeeds. While it held the window, C and B's landing waited.
    record, fakes, released = await batch(
        world, owner, clock, (third_runbook, third_task, second, second_sha), base_sha=case.base
    )
    assert (record.status, record.phase) == ("succeeded", None)
    assert fakes.observed == HELD
    assert [head for _name, head in fakes.checks] == [released] * len(record.check_plan)
    assert record.check_plan, "the cohort's Python changes choose a check plan"
    assert read_window(common) is None
    assert latest_baseline(common) == released

    # Release reopens both: C is admitted and B lands and closes.
    third = await seated_team(world, third_task, third_runbook)
    assert (await land(case, second, second_sha))["landed"] is True
    closed_second = close(case, second, second_sha)
    third_sha = reviewed_commit(case, third, "src/gobby/cohort_c.py")
    fourth_task = case.task("Cohort team D starts after the lease lapses")
    fourth_runbook = world.child(fourth_task)

    # The second batch fails a check. C kept working while D and C's landing waited.
    failed, fakes, candidate = await batch(
        world, owner, clock, (fourth_runbook, fourth_task, third, third_sha), fail="check"
    )
    assert (failed.status, failed.phase) == ("failed", "checks")
    assert fakes.observed == HELD
    assert (failed.base_sha, failed.candidate_sha) == (released, candidate)
    assert read_window(common) is None
    assert latest_baseline(common) == released
    # The failure files one new regression task and reopens no source task.
    label = f"release-regression-{candidate[:12]}"
    [regression] = tasks.list_tasks(project_id=case.project_id, label=label, closed=False)
    assert failed.regression_task == regression.id
    assert regression.id not in {first.task.id, second.task.id}
    for before in (closed_first, closed_second):
        after = tasks.get_task(before.id)
        assert before.closed_at is not None
        assert (after.closed_at, after.closed_commit_sha, after.commits) == (
            before.closed_at,
            before.closed_commit_sha,
            before.commits,
        )

    # A lapsed lease still on disk holds nothing: D is admitted and C lands.
    lapsed = await acquire_window(
        case.db,
        project_id=case.project_id,
        owner_session_id=owner,
        now=datetime.now(UTC) - LEASE_TTL - timedelta(seconds=1),
    )
    assert read_window(common) == lapsed
    fourth = await seated_team(world, fourth_task, fourth_runbook)
    assert Path(fourth.binding.worktree_path).is_dir()
    assert (await land(case, third, third_sha))["landed"] is True
    assert case.git("merge-base", "--is-ancestor", third_sha, "trunk") == ""
