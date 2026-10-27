"""Behavioral contract for the close invariants and required step records."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest

from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.tasks import LocalTaskManager, TaskHasOpenChildrenError, TaskStaleStateError


def git(repo: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()


@pytest.fixture
def repository(tmp_path: Path) -> tuple[Path, str, str]:
    git(tmp_path, "init", "-b", "integration")
    git(tmp_path, "config", "user.name", "Close test")
    git(tmp_path, "config", "user.email", "close@example.invalid")
    (tmp_path / "owned.txt").write_text("base\n")
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-m", "base")
    landed = git(tmp_path, "rev-parse", "HEAD")
    git(tmp_path, "checkout", "-b", "task")
    (tmp_path / "owned.txt").write_text("task\n")
    git(tmp_path, "commit", "-am", "task")
    return tmp_path, landed, git(tmp_path, "rev-parse", "HEAD")


def test_close_has_exactly_five_gates() -> None:
    from gobby.mcp_proxy.tools.tasks._lifecycle_close_preview import CLOSE_GATE_ORDER

    assert tuple(name for _, name in CLOSE_GATE_ORDER) == (
        "children_closed",
        "linked_commits",
        "uncommitted_task_edits",
        "integration_ancestry",
        "step_records",
    )


@pytest.mark.parametrize(
    "children,commit,reason,dirty,failed",
    [
        (False, "landed", "done", False, ()),
        (True, "landed", "done", False, ("children_closed",)),
        (False, "none", "done", False, ("linked_commits",)),
        (False, "none", "moot", False, ()),
        (False, "none", "moot", True, ("uncommitted_task_edits",)),
        (False, "landed", "done", True, ("uncommitted_task_edits",)),
        (False, "unlanded", "done", False, ("integration_ancestry",)),
        (False, "invalid", "done", False, ("integration_ancestry",)),
        (False, "none", "superseded", False, ()),
        (False, "unlanded", "moot", False, ("integration_ancestry",)),
        (False, "unlanded", "superseded", False, ("integration_ancestry",)),
        (False, "unlanded", "wont_do", False, ("integration_ancestry",)),
        (False, "landed", "moot", False, ("integration_ancestry",)),
        (False, "landed", "superseded", False, ("integration_ancestry",)),
        (False, "landed", "wont_do", False, ("integration_ancestry",)),
    ],
)
@pytest.mark.asyncio
async def test_close_invariant_matrix(
    repository: tuple[Path, str, str],
    children: bool,
    commit: str,
    reason: str,
    dirty: bool,
    failed: tuple[str, ...],
) -> None:
    from gobby.tasks.close_invariants import evaluate_close_invariants

    repo, landed, unlanded = repository
    if dirty:
        (repo / "owned.txt").write_text("dirty\n")
    (repo / "foreign.txt").write_text("another task's work\n")
    commits = {"landed": [landed], "unlanded": [unlanded], "invalid": ["0" * 40], "none": []}
    result = await evaluate_close_invariants(
        open_children=["child"] if children else [],
        commit_shas=commits[commit],
        reason=reason,
        attributed_checkouts={str(repo): {"owned.txt"}},
        integration_repo_path=str(repo),
        integration_ref="refs/heads/integration",
        required_steps=(),
        missing_steps=(),
    )
    assert tuple(gate.name for gate in result.all_failures) == failed
    assert result.ready is (not failed)
    assert len(result.gates) == 5


@pytest.mark.parametrize("reason", ["moot", "superseded", "wont_do"])
@pytest.mark.asyncio
async def test_no_work_close_skips_commit_and_step_gates(reason: str) -> None:
    from gobby.tasks.close_invariants import evaluate_close_invariants

    # Nothing is linked, so there is no commit to check and no step records to judge:
    # gates 2, 4 and 5 are skipped, not passed or failed.
    result = await evaluate_close_invariants(
        open_children=[],
        commit_shas=[],
        reason=reason,
        attributed_checkouts={},
        integration_repo_path=None,
        integration_ref=None,
        required_steps=("implementation", "closing"),
        missing_steps=("implementation", "closing"),
    )
    assert {gate.item: gate.status for gate in result.gates} == {
        1: "passed",
        2: "skipped",
        3: "passed",
        4: "skipped",
        5: "skipped",
    }
    assert result.ready


@pytest.mark.asyncio
async def test_no_work_close_with_a_linked_commit_still_checks_ancestry(
    repository: tuple[Path, str, str],
) -> None:
    from gobby.tasks.close_invariants import evaluate_close_invariants

    repo, landed, _unlanded = repository
    result = await evaluate_close_invariants(
        open_children=[],
        commit_shas=[landed],
        reason="moot",
        attributed_checkouts={},
        integration_repo_path=str(repo),
        integration_ref="refs/heads/integration",
        required_steps=("closing",),
        missing_steps=("closing",),
    )
    statuses = {gate.item: gate.status for gate in result.gates}
    assert statuses[2] == "passed"
    assert statuses[4] == "failed"
    assert statuses[5] == "skipped"
    assert not result.ready


@pytest.mark.asyncio
async def test_all_linked_commits_must_be_landed(repository: tuple[Path, str, str]) -> None:
    from gobby.tasks.close_invariants import evaluate_close_invariants

    repo, landed, unlanded = repository
    result = await evaluate_close_invariants(
        open_children=[],
        commit_shas=[unlanded, landed],
        reason="done",
        attributed_checkouts={},
        integration_repo_path=str(repo),
        integration_ref="refs/heads/integration",
        required_steps=(),
        missing_steps=(),
    )
    assert [gate.name for gate in result.all_failures] == ["integration_ancestry"]


@pytest.mark.parametrize(
    "commits,message",
    [
        (
            ("unlanded",),
            "Unlink linked commits with unlink_commit before a no-work close.",
        ),
        (
            ("landed",),
            "Landed commits cannot close as superseded; close with reason done.",
        ),
        (
            ("unlanded", "landed"),
            "Landed commits cannot close as superseded; close with reason done.",
        ),
    ],
)
@pytest.mark.asyncio
async def test_no_work_close_refuses_every_linked_commit(
    repository: tuple[Path, str, str],
    commits: tuple[str, ...],
    message: str,
) -> None:
    from gobby.tasks.close_invariants import evaluate_close_invariants

    repo, landed, unlanded = repository
    shas = {"landed": landed, "unlanded": unlanded}
    result = await evaluate_close_invariants(
        open_children=[],
        commit_shas=[shas[name] for name in commits],
        reason="superseded",
        attributed_checkouts={},
        integration_repo_path=str(repo),
        integration_ref="refs/heads/integration",
        required_steps=(),
        missing_steps=(),
    )
    (gate,) = result.all_failures
    assert gate.name == "integration_ancestry"
    assert gate.message == message


@pytest.mark.asyncio
async def test_integration_tip_must_resolve(repository: tuple[Path, str, str]) -> None:
    from gobby.tasks.close_invariants import evaluate_close_invariants

    repo, landed, _ = repository
    result = await evaluate_close_invariants(
        open_children=[],
        commit_shas=[landed],
        reason="done",
        attributed_checkouts={},
        integration_repo_path=str(repo),
        integration_ref="refs/heads/missing",
        required_steps=(),
        missing_steps=(),
    )
    assert [gate.name for gate in result.all_failures] == ["integration_ancestry"]


def test_close_is_atomic_and_idempotent(
    repository: tuple[Path, str, str],
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    repo, landed, _ = repository
    manager = LocalTaskManager(temp_db)
    task = manager.create_task(sample_project["id"], "Atomic close", task_type="epic")
    child = manager.create_task(
        sample_project["id"],
        "Child",
        parent_task_id=task.id,
        validation_criteria="Observable completion.",
    )
    with pytest.raises(TaskHasOpenChildrenError):
        manager.close_task_with_commit(task.id, landed, cwd=repo)
    rejected = manager.get_task(task.id)
    assert rejected.closed_at is None
    assert not rejected.commits
    manager.close_task(child.id, reason="moot")
    # The unclaimed structural parent may already have auto-closed. A fresh leaf
    # exercises the explicit commit/link transition and its repeated call.
    leaf = manager.create_task(
        sample_project["id"], "Leaf", validation_criteria="Observable completion."
    )
    closed = manager.close_task_with_commit(leaf.id, landed, cwd=repo)
    repeated = manager.close_task_with_commit(
        leaf.id, landed, cwd=repo, expected_updated_at=leaf.updated_at
    )
    assert closed.closed_at is not None
    assert repeated.closed_at == closed.closed_at
    assert repeated.updated_at == closed.updated_at
    assert repeated.commits == closed.commits
    assert len(closed.commits or []) == 1


def test_closing_note_is_written_only_with_its_close(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    manager = LocalTaskManager(temp_db)
    kept = manager.create_task(
        sample_project["id"], "Kept", validation_criteria="Observable completion."
    )
    noted = manager.create_task(
        sample_project["id"], "Noted", validation_criteria="Observable completion."
    )
    manager.close_task(kept.id, reason="moot")
    manager.close_task(
        noted.id,
        reason="superseded",
        closing_note="closed as superseded; superseded by #24970",
    )
    rows = temp_db.fetchall(
        "SELECT task_id, reason FROM task_lifecycle_events WHERE to_state = 'closed' ORDER BY id"
    )
    assert [(str(row["task_id"]), row["reason"]) for row in rows] == [
        (noted.id, "closed as superseded; superseded by #24970")
    ]


def test_stale_close_does_not_link_commit(
    repository: tuple[Path, str, str],
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    repo, landed, _ = repository
    manager = LocalTaskManager(temp_db)
    task = manager.create_task(
        sample_project["id"], "Changed task", validation_criteria="Observable completion."
    )
    manager.add_label(task.id, "changed")
    with pytest.raises(TaskStaleStateError):
        manager.close_task_with_commit(
            task.id, landed, cwd=repo, expected_updated_at=task.updated_at
        )
    unchanged = manager.get_task(task.id)
    assert unchanged.closed_at is None
    assert not unchanged.commits


@pytest.mark.asyncio
async def test_close_needs_no_evidence_relay(
    temp_db: HubDatabase,
    sample_git_project: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from gobby.mcp_proxy.tools.internal import InternalToolRegistry
    from gobby.mcp_proxy.tools.tasks._context import RegistryContext
    from gobby.mcp_proxy.tools.tasks._lifecycle_close_tool import register_close_task
    from gobby.utils.machine_id import require_machine_id

    repo = Path(sample_git_project["repo_path"])
    (repo / "work.txt").write_text("complete\n")
    git(repo, "add", "work.txt")
    git(repo, "commit", "-m", "completed work")
    sha = git(repo, "rev-parse", "HEAD")
    manager = LocalTaskManager(temp_db)
    ctx = RegistryContext(task_manager=manager)
    session = ctx.session_manager.register(
        external_id="four-invariant-close",
        machine_id=require_machine_id(),
        source="codex",
        project_id=sample_git_project["id"],
    )
    task = manager.create_task(
        sample_git_project["id"], "No relay", validation_criteria="Live: ordinary reviewer text."
    )
    task = manager.claim_task(task.id, session.id)
    monkeypatch.setattr(
        "gobby.mcp_proxy.tools.tasks._lifecycle_close.get_current_session_id", lambda: session.id
    )
    evidence = Mock(side_effect=AssertionError("close must not read transcript evidence"))
    monkeypatch.setattr("gobby.tasks.transcript_evidence.derive_transcript_evidence", evidence)
    registry = InternalToolRegistry("gobby-tasks")
    register_close_task(registry, ctx)
    preview = await registry.call(
        "close_task",
        {
            "task_id": task.id,
            "commit_sha": sha,
            "preview": True,
        },
    )
    assert preview["can_close"] is True, (preview.get("error"), preview.get("gates"))
    assert len(preview["gates"]) == 5
    pending = manager.get_task(task.id)
    assert pending.closed_at is None and not pending.commits
    closed = await registry.call("close_task", {"task_id": task.id, "commit_sha": sha})
    assert closed["closed"] is True, closed
    assert closed["gates"] == preview["gates"]
    assert manager.get_task(task.id).closed_commit_sha == sha
    assert manager.get_task(task.id).review_status == task.review_status
    evidence.assert_not_called()


@pytest.mark.asyncio
async def test_close_uses_authoritative_integration_checkout(
    temp_db: HubDatabase,
    sample_git_project: dict[str, Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from gobby.mcp_proxy.tools.internal import InternalToolRegistry
    from gobby.mcp_proxy.tools.tasks._context import RegistryContext
    from gobby.mcp_proxy.tools.tasks._lifecycle_close_tool import register_close_task
    from gobby.utils.machine_id import require_machine_id

    main = Path(sample_git_project["repo_path"])
    clone = tmp_path / "clone"
    git(main, "clone", str(main), str(clone))
    git(clone, "config", "user.name", "Close test")
    git(clone, "config", "user.email", "close@example.invalid")
    branch = git(main, "symbolic-ref", "HEAD")
    base = git(main, "rev-parse", "HEAD")
    (clone / "work.txt").write_text("clone-only work\n")
    git(clone, "add", "work.txt")
    git(clone, "commit", "-m", "clone work")
    sha = git(clone, "rev-parse", "HEAD")
    manager = LocalTaskManager(temp_db)
    ctx = RegistryContext(task_manager=manager)
    session = ctx.session_manager.register(
        external_id="clone-close",
        machine_id=require_machine_id(),
        source="codex",
        project_id=sample_git_project["id"],
    )
    task = manager.create_task(
        sample_git_project["id"], "Clone close", validation_criteria="Landed work."
    )
    manager.claim_task(task.id, session.id)
    monkeypatch.setattr(
        "gobby.mcp_proxy.tools.tasks._lifecycle_close.get_current_session_id", lambda: session.id
    )
    # Exercise an authorized clone at the repository-resolution boundary.
    monkeypatch.setattr(
        "gobby.mcp_proxy.tools.tasks._lifecycle_close.resolve_task_repo_path",
        lambda **kwargs: str(clone),
    )
    registry = InternalToolRegistry("gobby-tasks")
    register_close_task(registry, ctx)
    arguments = {"task_id": task.id, "commit_sha": sha, "project_path": str(clone)}
    for preview in (True, False):
        result = await registry.call("close_task", {**arguments, "preview": preview})
        assert result["can_close"] is False, result
        assert result["error"] == "integration_ancestry"
    assert manager.get_task(task.id).closed_at is None
    assert not manager.get_task(task.id).commits

    git(main, "fetch", str(clone), branch)
    git(main, "merge", "--ff-only", "FETCH_HEAD")
    git(clone, "checkout", "--detach", sha)
    git(clone, "update-ref", branch, base)
    preview = await registry.call("close_task", {**arguments, "preview": True})
    assert preview["can_close"] is True, preview
    closed = await registry.call("close_task", arguments)
    assert closed["closed"] is True, closed


@pytest.mark.parametrize("explicit_main", [False, True])
@pytest.mark.asyncio
async def test_no_work_close_checks_dirty_task_checkout(
    temp_db: HubDatabase,
    sample_git_project: dict[str, Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    explicit_main: bool,
) -> None:
    from gobby.mcp_proxy.tools.internal import InternalToolRegistry
    from gobby.mcp_proxy.tools.tasks._context import RegistryContext
    from gobby.mcp_proxy.tools.tasks._lifecycle_close_tool import register_close_task
    from gobby.utils.machine_id import require_machine_id

    main = Path(sample_git_project["repo_path"])
    worktree = tmp_path / "dirty-task"
    git(main, "worktree", "add", "-b", "dirty-task", str(worktree))
    (worktree / "owned.txt").write_text("uncommitted task work\n")
    manager = LocalTaskManager(temp_db)
    ctx = RegistryContext(task_manager=manager)
    session = ctx.session_manager.register(
        external_id=f"no-work-{explicit_main}",
        machine_id=require_machine_id(),
        source="codex",
        project_id=sample_git_project["id"],
    )
    task = manager.create_task(
        sample_git_project["id"], "Dirty no-work", validation_criteria="Task-owned work."
    )
    manager.claim_task(task.id, session.id)
    ctx.worktree_manager.create(
        project_id=task.project_id,
        branch_name="dirty-task",
        worktree_path=str(worktree),
        task_id=task.id,
    )
    ctx.session_var_manager.merge_variables(
        session.id,
        {
            "claimed_tasks": {task.id: f"#{task.seq_num}"},
            "task_claimed": True,
            "task_edited_files": {task.id: ["owned.txt"]},
            "task_edited_file_checkouts": {task.id: {str(worktree): ["owned.txt"]}},
            "session_dirty_files": ["owned.txt"],
            "session_dirty_file_checkouts": {str(worktree): ["owned.txt"]},
        },
    )
    monkeypatch.setattr(
        "gobby.mcp_proxy.tools.tasks._lifecycle_close.get_current_session_id", lambda: session.id
    )
    registry = InternalToolRegistry("gobby-tasks")
    register_close_task(registry, ctx)
    args: dict[str, Any] = {"task_id": task.id, "reason": "moot"}
    if explicit_main:
        args["project_path"] = str(main)
    for preview in (True, False):
        result = await registry.call("close_task", {**args, "preview": preview})
        assert result["can_close"] is False, result
        assert result["error"] == "uncommitted_task_edits"
    assert manager.get_task(task.id).closed_at is None


_POLICY_TIP = "5ca1ab1" + "e" * 33


def _set_close_policy(db: HubDatabase, project_id: str, policy: dict[str, Any]) -> None:
    """Write the policy into the project's primary checkout marker."""
    from gobby.storage.project_checkouts import require_root
    from gobby.utils.machine_id import require_machine_id

    marker = Path(require_root(db, project_id, require_machine_id())) / ".gobby" / "project.json"
    data = json.loads(marker.read_text(encoding="utf-8"))
    marker.write_text(json.dumps({**data, "task_close_policy": policy}), encoding="utf-8")


@pytest.mark.asyncio
async def test_unlinked_superseded_task_closes_with_no_work_records(
    temp_db: HubDatabase, sample_project: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    from gobby.storage.tasks._lifecycle_events import TaskLifecycleEventManager
    from gobby.tasks.close_invariants import evaluate_close_invariants
    from gobby.tasks.commit_unlink import unlink_commit_audited
    from gobby.tasks.step_records import (
        StepSubmission,
        TaskStep,
        record_task_step,
        task_step_requirements,
    )
    from tests.tasks.test_step_records import _Seats

    monkeypatch.setattr(
        "gobby.tasks.land_commit.main_checkout_target",
        AsyncMock(side_effect=ValueError("no registered checkout")),
    )
    project_id = sample_project["id"]
    manager = LocalTaskManager(temp_db)
    _set_close_policy(temp_db, project_id, {"enforce_step_records": True})
    seats = _Seats(temp_db, project_id)
    developer = seats.add("superseded-developer", ["implementation"])
    reviewer = seats.add("superseded-reviewer", ["review"])
    closer = seats.add("superseded-closer", ["closing"])
    task = manager.create_task(
        project_id, "Superseded candidate", category="research", validation_criteria="Moot."
    )
    candidate = "a" * 40
    manager.link_commit(task.id, candidate)
    record_task_step(
        temp_db,
        task_id=task.id,
        session_id=developer,
        step="implementation",
        submission=StepSubmission(
            status="pending",
            epoch=1,
            round_number=1,
            feedback="The implementation is pending.",
            commit_sha=candidate,
        ),
    )
    assert "implementation" in task_step_requirements(temp_db, manager.get_task(task.id)).missing

    await unlink_commit_audited(
        manager,
        manager.get_task(task.id),
        candidate,
        reason="Superseded by #24970",
        actor=developer,
    )
    unlinked = manager.get_task(task.id)
    assert not unlinked.commits
    approvals: tuple[tuple[TaskStep, str], ...] = (
        ("implementation", developer),
        ("review", reviewer),
        ("closing", closer),
    )
    for step, session_id in approvals:
        record_task_step(
            temp_db,
            task_id=task.id,
            session_id=session_id,
            step=step,
            submission=StepSubmission(
                status="approved",
                epoch=1,
                round_number=1,
                feedback=f"The {step} step closes as superseded.",
                disposition="superseded",
            ),
        )
    requirements = task_step_requirements(temp_db, unlinked)
    checklist = await evaluate_close_invariants(
        open_children=[],
        commit_shas=[],
        reason="superseded",
        attributed_checkouts={},
        integration_repo_path=None,
        integration_ref=None,
        required_steps=requirements.required,
        missing_steps=requirements.missing,
    )
    assert checklist.ready, requirements.missing

    closed = manager.close_task(
        task.id,
        reason="superseded",
        closed_ancestors=[],
        closing_note="closed as superseded; superseded by #24970",
    )
    assert closed.closed_at is not None
    events = TaskLifecycleEventManager(temp_db).list_lifecycle_events(task.id)
    unlinks = [event for event in events if event.to_state == "commit:unlinked"]
    assert [(event.by_actor, event.reason) for event in unlinks] == [
        (developer, f"Unlinked {candidate}: Superseded by #24970")
    ]
    closing = [event for event in events if event.to_state == "closed"]
    assert [event.reason for event in closing] == ["closed as superseded; superseded by #24970"]


def test_last_child_close_leaves_a_parent_without_records_open(
    temp_db: HubDatabase, sample_project: dict[str, Any]
) -> None:
    from gobby.tasks.step_records import StepSubmission, TaskStep, record_task_step
    from tests.storage.tasks.test_legacy_validation_criteria_close import _make_legacy
    from tests.tasks.test_step_records import _Seats

    project_id = sample_project["id"]
    manager = LocalTaskManager(temp_db)
    _set_close_policy(temp_db, project_id, {"enforce_step_records": True})
    seats = _Seats(temp_db, project_id)
    writers: dict[TaskStep, str] = {
        "implementation": seats.add("policy-developer", ["implementation"]),
        "review": seats.add("policy-reviewer", ["review"]),
        "closing": seats.add("policy-closer", ["closing"]),
    }
    # A legacy row with no criteria and no hold: only its own missing records keep it open.
    parent = manager.create_task(
        project_id, "Unrecorded parent", category="research", validation_criteria="Legacy."
    )
    _make_legacy(temp_db, parent.id)
    child = manager.create_task(
        project_id,
        "Last child",
        category="research",
        parent_task_id=parent.id,
        validation_criteria="Moot.",
    )
    for step, session_id in writers.items():
        record_task_step(
            temp_db,
            task_id=child.id,
            session_id=session_id,
            step=step,
            submission=StepSubmission(
                status="approved",
                epoch=1,
                round_number=1,
                feedback=f"The {step} step is approved.",
                commit_sha=None,
                disposition="moot",
            ),
        )
    closed_ancestors: list[str] = []

    closed = manager.close_task(child.id, reason="moot", closed_ancestors=closed_ancestors)

    assert closed.closed_at is not None
    assert closed_ancestors == []
    assert manager.get_task(parent.id).closed_at is None


@pytest.mark.asyncio
async def test_required_step_records_follow_project_policy(
    temp_db: HubDatabase, sample_project: dict[str, Any]
) -> None:
    from gobby.config.tasks import TaskClosePolicyConfig
    from gobby.storage.tasks import Task, TaskStepRecordsMissingError
    from gobby.tasks.close_invariants import evaluate_close_invariants
    from gobby.tasks.step_records import (
        StepSubmission,
        TaskStep,
        record_task_step,
        required_task_steps,
        task_step_requirements,
    )
    from tests.tasks.test_step_records import _Seats

    enforced = TaskClosePolicyConfig(enforce_step_records=True)
    tested = TaskClosePolicyConfig(
        enforce_step_records=True, test_categories=["code"], review_categories=["code"]
    )
    matrix: list[tuple[TaskClosePolicyConfig, str, str | None]] = [
        (TaskClosePolicyConfig(), "task", "code"),
        (enforced, "task", "code"),
        (enforced, "chore", "docs"),
        (enforced, "research_spike", "research"),
        (enforced, "task", None),
        (enforced, "epic", "code"),
        (tested, "bug", "code"),
        (tested, "chore", "docs"),
        (tested, "epic", "code"),
    ]
    required = [
        required_task_steps(policy, task_type=kind, category=area) for policy, kind, area in matrix
    ]
    assert required == [
        (),
        ("implementation", "review", "closing"),
        ("implementation", "review", "closing"),
        ("implementation", "review", "closing"),
        ("implementation", "closing"),
        (),
        ("test", "implementation", "review", "closing"),
        ("implementation", "closing"),
        (),
    ]
    with pytest.raises(ValueError, match="unknown task category"):
        TaskClosePolicyConfig(review_categories=["cod"])

    project_id = sample_project["id"]
    manager = LocalTaskManager(temp_db)
    seats = _Seats(temp_db, project_id)
    developer = seats.add("policy-developer", ["test", "implementation"])
    reviewer = seats.add("policy-reviewer", ["review"])
    closer = seats.add("policy-closer", ["closing"])

    def record(task: Task, session_id: str, step: TaskStep, *, linked: bool = True) -> None:
        record_task_step(
            temp_db,
            task_id=task.id,
            session_id=session_id,
            step=step,
            submission=StepSubmission(
                status="approved",
                epoch=1,
                round_number=1,
                feedback=f"The {step} step is approved.",
                commit_sha=_POLICY_TIP if linked else None,
                disposition=None if linked else "moot",
            ),
        )

    def work_task(title: str, category: str, parent_id: str | None = None) -> Task:
        created = manager.create_task(
            project_id,
            title,
            category=category,
            parent_task_id=parent_id,
            validation_criteria="Project policy selects the approved step records.",
        )
        manager.claim_task(created.id, developer)
        manager.link_commit(created.id, _POLICY_TIP[:10])
        return manager.get_task(created.id)

    unenforced = work_task("Policy off", "code")
    assert task_step_requirements(temp_db, unenforced).required == ()
    assert manager.close_task(unenforced.id).closed_at is not None

    _set_close_policy(temp_db, project_id, {"enforce_step_records": True})
    epic = manager.create_task(project_id, "Policy epic", task_type="epic")
    work = work_task("Policy code", "code", epic.id)
    with pytest.raises(TaskStepRecordsMissingError, match="implementation, review, closing"):
        manager.close_task(work.id)
    record(work, developer, "implementation")
    record(work, reviewer, "review")
    requirements = task_step_requirements(temp_db, manager.get_task(work.id))
    assert (requirements.required, requirements.missing) == (
        ("implementation", "review", "closing"),
        ("closing",),
    )
    preview = await evaluate_close_invariants(
        open_children=[],
        commit_shas=[],
        reason="moot",
        attributed_checkouts={},
        integration_repo_path=None,
        integration_ref=None,
        required_steps=requirements.required,
        missing_steps=requirements.missing,
    )
    gate = preview.gates[-1]
    assert (gate.item, gate.name, gate.status, gate.details["missing_steps"]) == (
        5,
        "step_records",
        "failed",
        ["closing"],
    )
    assert [failure.name for failure in preview.all_failures] == ["step_records"]
    with pytest.raises(TaskStepRecordsMissingError, match="closing"):
        manager.close_task(work.id, force=True)
    record(work, closer, "closing")
    closed_ancestors: list[str] = []
    assert manager.close_task(work.id, closed_ancestors=closed_ancestors).closed_at is not None
    assert closed_ancestors == [epic.id]

    research = manager.create_task(
        project_id, "Policy research", category="research", validation_criteria="Moot."
    )
    with pytest.raises(TaskStepRecordsMissingError, match="implementation, review, closing"):
        manager.close_task(research.id, reason="moot")
    record(research, developer, "implementation", linked=False)
    record(research, reviewer, "review", linked=False)
    record(research, closer, "closing", linked=False)
    assert manager.close_task(research.id, reason="moot").closed_at is not None

    _set_close_policy(
        temp_db, project_id, {"enforce_step_records": True, "test_categories": ["code"]}
    )
    test_first = work_task("Policy tested", "code")
    record(test_first, developer, "test")
    record(test_first, developer, "implementation")
    record(test_first, reviewer, "review")
    record(test_first, closer, "closing")
    assert task_step_requirements(temp_db, manager.get_task(test_first.id)).missing == ()
    assert manager.close_task(test_first.id).closed_at is not None
    untested = work_task("Policy untested", "code")
    record(untested, developer, "implementation")
    record(untested, reviewer, "review")
    record(untested, closer, "closing")
    with pytest.raises(TaskStepRecordsMissingError, match="test"):
        manager.close_task(untested.id)

    _set_close_policy(temp_db, project_id, {"enforce": True})
    with pytest.raises(ValueError, match="task_close_policy"):
        task_step_requirements(temp_db, manager.get_task(untested.id))
    with pytest.raises(ValueError, match="task_close_policy"):
        manager.close_task(untested.id)
