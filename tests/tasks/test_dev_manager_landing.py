"""Dev Manager landing authority, conflict escalation and task worktree marking."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from gobby.storage.agents import LocalAgentRunManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.tasks import LocalTaskManager
from gobby.storage.worktrees import LocalWorktreeManager, WorktreeStatus
from gobby.tasks.close_receipts import LANDING, list_close_receipts
from gobby.tasks.landing_policy import write_freeze
from gobby.tasks.step_records import StepSubmission, record_task_step
from tests.fixtures.landing_case import DEV_MANAGER, LandingCase, make_landing_case
from tests.fixtures.review_approvals import approve_review, grant_review_writes, grant_step_writes

pytestmark = pytest.mark.unit

REFUSALS: dict[str, list[str]] = {
    "unrelated_manager": ["caller_not_task_manager"],
    "unbound_closer": ["caller_not_task_manager"],
    # A researcher's closing grant closes its own spike; it never manages a task.
    "bound_researcher": ["caller_not_task_manager"],
    "reviewer": ["caller_not_task_manager", "caller_is_reviewer", "review_approval_missing"],
    "self_reviewing_manager": ["caller_is_reviewer", "review_approval_missing"],
    "self_reviewing_orchestrator": ["caller_is_reviewer", "review_approval_missing"],
    "claimant_manager": ["caller_is_claimant"],
    "missing_approval": ["review_approval_missing"],
}


@pytest.fixture
def case(temp_db: HubDatabase, sample_project: dict[str, Any], tmp_path: Path) -> LandingCase:
    return make_landing_case(temp_db, sample_project, tmp_path)


@pytest.mark.parametrize("caller_kind", ["bound_manager", "orchestrator", *REFUSALS])
async def test_manager_landing_authority(case: LandingCase, caller_kind: str) -> None:
    sha = case.candidate("lane", {"docs/change.md": "land me\n"})
    task = case.task()
    case.link(task, sha)
    case.implement(task, sha)
    if caller_kind in {
        "bound_manager",
        "unrelated_manager",
        "unbound_closer",
        "bound_researcher",
        "reviewer",
    }:
        case.approve(task, sha)
    if caller_kind in {"orchestrator", "claimant_manager"}:
        case.approve(task, sha)
    if caller_kind in {"bound_manager", "missing_approval"}:
        caller = case.manager_for(task)
    elif caller_kind == "orchestrator":
        caller = case.orchestrator()
    elif caller_kind == "unrelated_manager":
        caller = case.manager_for(case.task("Another manager's task"))
    elif caller_kind == "unbound_closer":
        caller = case.session("closer without a run", parent=case.creator)
        grant_step_writes(case.db, caller, case.project_id, DEV_MANAGER, ("closing",))
    elif caller_kind == "bound_researcher":
        caller = case.session("researcher", parent=case.creator)
        grant_step_writes(
            case.db, caller, case.project_id, "researcher", ("implementation", "closing")
        )
        LocalAgentRunManager(case.db).create(
            parent_session_id=case.creator,
            provider="claude",
            prompt="Research the spike.",
            agent_name="researcher",
            child_session_id=caller,
            task_id=task.id,
        )
    elif caller_kind == "reviewer":
        caller = case.reviewer
    elif caller_kind == "self_reviewing_manager":
        caller = case.manager_for(task)
        grant_step_writes(
            case.db, caller, case.project_id, "fixture-reviewing-manager", ("review", "closing")
        )
        approve_review(case.db, task_id=task.id, reviewer_session_id=caller, commit_sha=sha)
    elif caller_kind == "self_reviewing_orchestrator":
        caller = case.orchestrator()
        approve_review(case.db, task_id=task.id, reviewer_session_id=caller, commit_sha=sha)
    else:
        caller = case.manager_for(task, session=case.claimant)

    result = await case.land(task, sha, caller=caller)

    after = LocalTaskManager(case.db).get_task(task.id)
    assert after.is_escalated is False
    assert after.claimed_by_session_id == case.claimant
    landings = [r for r in list_close_receipts(case.db, task.id) if r.kind == LANDING]
    if caller_kind in REFUSALS:
        assert result["landed"] is False
        assert result["blockers"] == REFUSALS[caller_kind]
        assert result["error"] == REFUSALS[caller_kind][0]
        assert "escalated" not in result
        assert case.git("rev-parse", "trunk") == case.base
        assert landings == []
        return
    assert result["landed"] is True, result
    assert result["landed_by_session_id"] == caller
    assert case.git("rev-parse", "trunk") == sha
    [receipt] = landings
    assert receipt.author_session_id == caller


@pytest.mark.parametrize("implementation", ["missing", "stale", "rejected", "approved"])
async def test_test_review_alone_cannot_land(case: LandingCase, implementation: str) -> None:
    task = case.task()
    manager = case.manager_for(task)
    first = case.candidate("first", {"feature.py": "initial = True\n"})
    sha = case.candidate("tests", {"tests/test_feature.py": "assert True\n"}, base=first)
    case.link(task, first)
    grant_step_writes(case.db, case.claimant, case.project_id, "developer", ("implementation",))

    def implement(commit: str) -> None:
        record_task_step(
            case.db,
            task_id=task.id,
            session_id=case.claimant,
            step="implementation",
            submission=StepSubmission(
                status="rejected" if implementation == "rejected" else "approved",
                epoch=task.review_epoch,
                round_number=1,
                feedback="Implementation evidence for the candidate.",
                commit_sha=commit,
            ),
        )

    if implementation == "stale":
        implement(first)
    case.link(task, sha)
    if implementation in {"rejected", "approved"}:
        implement(sha)
    grant_review_writes(case.db, case.reviewer, case.project_id)
    approve_review(case.db, task_id=task.id, reviewer_session_id=case.reviewer, commit_sha=sha)

    result = await case.land(task, sha, caller=manager)

    if implementation == "approved":
        assert result["landed"] is True, result
        assert case.git("rev-parse", "trunk") == sha
    else:
        assert result["landed"] is False, result
        assert "implementation_approval_missing" in result["blockers"]
        assert case.git("rev-parse", "trunk") == case.base
        assert not [r for r in list_close_receipts(case.db, task.id) if r.kind == LANDING]


@pytest.mark.parametrize(
    "scenario",
    ["disjoint", "shared_paths", "in_flight_peer", "frozen_conflict", "unauthorized_conflict"],
)
async def test_disjoint_and_overlapping_landings(case: LandingCase, scenario: str) -> None:
    if scenario == "in_flight_peer":
        # Only the integration tip counts: an unlanded peer never blocks the first lander.
        sha = case.candidate("lane", {"docs/shared.md": "lane\n"})
        other = case.candidate("other", {"docs/shared.md": "other\n"})
        other_task = case.reviewed(other, "Other in-flight candidate")
        tip = case.base
    else:
        sha = case.candidate("lane", {"docs/shared.md": "lane\n", "tests/test_lane.py": "x = 1\n"})
        moved = "docs/moved.md" if scenario == "disjoint" else "docs/shared.md"
        tip = case.direct({moved: "moved\n"})
    task = case.reviewed(sha)
    if scenario == "frozen_conflict":
        write_freeze(case.repo / ".git", on=True, reason="release window", session_id=case.creator)
    caller = case.delegator if scenario == "unauthorized_conflict" else case.manager_for(task)

    result = await case.land(task, sha, caller=caller)

    after = LocalTaskManager(case.db).get_task(task.id)
    if scenario == "in_flight_peer":
        assert (result["landed"], result["mode"]) == (True, "ff"), result
        assert "overlaps" not in result
        # The later lander's path changed in between, so it escalates against the tip.
        later = await case.land(other_task, other)
        assert later["blockers"] == ["base_update_required"]
        assert later["escalated"] is True
        assert case.git("rev-parse", "trunk") == sha
        return
    if scenario == "disjoint":
        merge = case.git("rev-parse", "trunk")
        assert result["landed"] is True, result
        assert (result["mode"], result["retest_required"]) == ("merge", False)
        assert "retest_procedure" not in result
        assert result["landed_tip"] == result["merge_commit"] == merge
        assert case.git("rev-list", "--parents", "-n", "1", merge).split() == [merge, tip, sha]
        assert (case.repo / "docs/shared.md").read_text() == "lane\n"
        assert after.is_escalated is False
        return
    expected = {
        "shared_paths": ["base_update_required"],
        "frozen_conflict": ["landing_frozen", "base_update_required"],
        "unauthorized_conflict": ["caller_not_task_manager", "base_update_required"],
    }[scenario]
    assert result["landed"] is False
    assert result["blockers"] == expected
    assert case.git("rev-parse", "trunk") == tip
    assert result["shared_paths"] == ["docs/shared.md"]
    if scenario in {"frozen_conflict", "unauthorized_conflict"}:
        assert "escalated" not in result
        assert after.is_escalated is False
        assert after.claimed_by_session_id == case.claimant
        return
    assert result["escalated"] is True
    assert after.is_escalated is True
    assert after.claimed_by_session_id is None
    assert json.loads(after.escalation_reason or "") == {
        "kind": "landing_conflict",
        "commit_sha": sha,
        "blockers": expected,
        "shared_paths": ["docs/shared.md"],
    }


@pytest.mark.parametrize(
    "scenario",
    [
        "ff",
        "replay",
        "stale_branch",
        "head_lacks_sha",
        "head_ahead",
        "restart_class",
        "schema_carrier_missing",
        "project_sync",
    ],
)
async def test_landing_marks_bound_worktree(
    case: LandingCase, tmp_path: Path, scenario: str
) -> None:
    files = {
        "restart_class": {"src/gobby/example.py": "example = 1\n"},
        "schema_carrier_missing": {
            "crates/gcore/assets/schema/migrations/999-test.sql": "SELECT 1;\n"
        },
        "project_sync": {".gobby/workflows/rules/operating-policy.yaml": "name: example\n"},
    }.get(scenario, {"docs/change.md": "land me\n"})
    sha = case.candidate("lane", files)
    task = case.reviewed(sha)
    tree = tmp_path / "task-tree"
    if scenario == "head_lacks_sha":
        case.git("worktree", "add", "-b", "elsewhere", str(tree), case.base)
    else:
        case.git("worktree", "add", str(tree), "lane")
    if scenario == "head_ahead":
        (tree / "docs" / "later.md").write_text("unlanded\n")
        case.git("-C", str(tree), "add", "docs/later.md")
        case.git("-C", str(tree), "commit", "-m", "later work")
    worktrees = LocalWorktreeManager(case.db)
    row = worktrees.create(
        case.project_id,
        "renamed-lane" if scenario == "stale_branch" else "lane",
        str(tree),
        base_branch="trunk",
        task_id=task.id,
    )

    result = await case.land(task, sha)
    marked = worktrees.get(row.id)
    assert marked is not None

    if scenario == "schema_carrier_missing":
        assert result["landed"] is False
        assert result["blockers"] == ["schema_carriers_missing"]
        assert result["missing_schema_carriers"] == [
            "src/gobby/storage/schema_expected_identity.json"
        ]
        assert marked.status == WorktreeStatus.ACTIVE.value
        assert LocalTaskManager(case.db).get_task(task.id).is_escalated is False
        return
    assert result["landed"] is True, result
    assert case.git("rev-parse", "trunk") == sha
    assert all(r.kind == LANDING for r in list_close_receipts(case.db, task.id))
    if scenario in {"head_lacks_sha", "head_ahead"}:
        assert result["worktree_merged"] is False
        assert marked.status == WorktreeStatus.ACTIVE.value
        assert marked.merged_at is None
        return
    assert result["worktree_merged"] is True
    assert marked.status == WorktreeStatus.MERGED.value
    assert marked.branch_name == "lane"
    assert marked.merged_at is not None
    assert marked.cleanup_after is not None and marked.cleanup_after <= marked.merged_at
    assert result["activation_class"] == {
        "restart_class": "restart",
        "project_sync": "restart",
    }.get(scenario, "none")
    assert "project_sync_required" not in result
    if scenario == "replay":
        replay = await case.land(task, sha, caller=case.orchestrator())
        again = worktrees.get(row.id)
        assert again is not None
        assert (replay["already_landed"], replay["worktree_merged"]) == (True, True)
        assert again.merged_at == marked.merged_at
