from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from gobby.build.dispatch_tick import DispatcherTickSummary
from gobby.build.options import BuildOptions
from gobby.build.service import build
from gobby.build.workspaces import (
    BuildWorkspaceError,
    _integration_branch,
    _refresh_clean_git_dir,
    ensure_epic_integration_workspaces,
)
from gobby.storage.projects import LocalProjectManager
from gobby.storage.tasks import LocalTaskManager
from gobby.storage.worktrees import LocalWorktreeManager

pytestmark = pytest.mark.unit


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def _is_ancestor(cwd: Path, ancestor: str) -> bool:
    result = subprocess.run(
        ["git", "merge-base", "--is-ancestor", ancestor, "HEAD"],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode == 0


def _init_repo(path: Path) -> None:
    _git(path, "init", "-b", "main")
    _git(path, "config", "user.email", "test@example.com")
    _git(path, "config", "user.name", "Test User")
    (path / "README.md").write_text("initial\n")
    _git(path, "add", "README.md")
    _git(path, "commit", "-m", "initial")


@pytest.mark.asyncio
async def test_child_build_resume_repairs_parent_integration_metadata_only(
    monkeypatch: pytest.MonkeyPatch,
    temp_db,
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    integration_path = tmp_path / "integration"
    task_path = tmp_path / "task"
    repo.mkdir()
    _init_repo(repo)

    project = LocalProjectManager(temp_db).create("merge-project", repo_path=str(repo))
    task_manager = LocalTaskManager(temp_db)
    parent = task_manager.create_task(project_id=project.id, title="Parent", task_type="epic")
    leaf = task_manager.create_task(
        project_id=project.id,
        title="Leaf",
        parent_task_id=parent.id,
        category="code",
        task_type="task",
    )
    sibling = task_manager.create_task(
        project_id=project.id,
        title="Sibling",
        parent_task_id=parent.id,
        category="code",
        task_type="task",
    )
    integration_branch = _integration_branch(parent)

    _git(repo, "worktree", "add", "-b", integration_branch, str(integration_path), "main")
    _git(repo, "worktree", "add", "-b", "task/leaf", str(task_path), integration_branch)
    task_manager.initialize_task_manifest(leaf.id, stage_names=["merge"])
    task_manager.stage_states.start_stage(leaf.id, "merge", by_session_id="test")
    task_manager.artifacts.set_artifacts_atomic(parent.id, target_branch="main")

    worktrees = LocalWorktreeManager(temp_db)
    source = worktrees.create(
        project_id=project.id,
        branch_name="task/leaf",
        worktree_path=str(task_path),
        base_branch=integration_branch,
        task_id=leaf.id,
    )
    task_manager.artifacts.set_artifacts_atomic(
        leaf.id,
        worktree_path=str(task_path),
        worktree_id=source.id,
        base_commit_sha=_git(repo, "rev-parse", "main"),
        target_branch=integration_branch,
    )

    async def fake_tick(*_args: object, **_kwargs: object) -> DispatcherTickSummary:
        return DispatcherTickSummary()

    monkeypatch.setattr("gobby.build.lifecycle._kick_dispatcher_tick", fake_tick)

    await build(
        str(leaf.seq_num),
        BuildOptions(isolation="worktree", target_branch="main"),
        db=temp_db,
        project_id=project.id,
    )

    parent_artifacts = task_manager.artifacts.get_artifacts(parent.id)
    adopted = worktrees.get_by_branch(project.id, integration_branch)

    assert adopted is not None
    assert adopted.workspace_role == "integration"
    assert adopted.worktree_path == str(integration_path)
    assert parent_artifacts.integration_workspace_id == adopted.id
    assert task_manager.stage_states.list_for_task(sibling.id) == []
    assert task_manager.get_task(sibling.id).allow_automation is False


@pytest.mark.asyncio
async def test_child_build_resume_restores_missing_leaf_target_branch(
    monkeypatch: pytest.MonkeyPatch,
    temp_db,
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    integration_path = tmp_path / "integration"
    task_path = tmp_path / "task"
    repo.mkdir()
    _init_repo(repo)

    project = LocalProjectManager(temp_db).create("merge-project", repo_path=str(repo))
    task_manager = LocalTaskManager(temp_db)
    parent = task_manager.create_task(project_id=project.id, title="Parent", task_type="epic")
    leaf = task_manager.create_task(
        project_id=project.id,
        title="Leaf",
        parent_task_id=parent.id,
        category="code",
        task_type="task",
    )
    integration_branch = _integration_branch(parent)
    base_sha = _git(repo, "rev-parse", "main")

    _git(repo, "worktree", "add", "-b", integration_branch, str(integration_path), "main")
    _git(repo, "worktree", "add", "-b", "task/leaf", str(task_path), integration_branch)
    _git(task_path, "config", "user.email", "test@example.com")
    _git(task_path, "config", "user.name", "Test User")
    (task_path / "feature.txt").write_text("feature\n")
    _git(task_path, "add", "feature.txt")
    _git(task_path, "commit", "-m", "feature")

    task_manager.initialize_task_manifest(leaf.id, stage_names=["merge"])
    worktrees = LocalWorktreeManager(temp_db)
    integration = worktrees.create(
        project_id=project.id,
        branch_name=integration_branch,
        worktree_path=str(integration_path),
        base_branch="main",
        task_id=parent.id,
        workspace_role="integration",
    )
    source = worktrees.create(
        project_id=project.id,
        branch_name="task/leaf",
        worktree_path=str(task_path),
        base_branch=integration_branch,
        task_id=leaf.id,
    )
    task_manager.artifacts.set_artifacts_atomic(
        parent.id,
        target_branch="main",
        integration_branch=integration_branch,
        integration_workspace_id=integration.id,
    )
    task_manager.artifacts.set_artifacts_atomic(
        leaf.id,
        worktree_path=str(task_path),
        worktree_id=source.id,
        base_commit_sha=base_sha,
    )

    async def fake_tick(*_args: object, **_kwargs: object) -> DispatcherTickSummary:
        return DispatcherTickSummary()

    monkeypatch.setattr("gobby.build.lifecycle._kick_dispatcher_tick", fake_tick)

    await build(
        str(leaf.seq_num),
        BuildOptions(isolation="worktree"),
        db=temp_db,
        project_id=project.id,
    )

    leaf_artifacts = task_manager.artifacts.get_artifacts(leaf.id)

    assert leaf_artifacts.target_branch == integration_branch
    assert leaf_artifacts.worktree_id == source.id
    assert leaf_artifacts.worktree_path == str(task_path)


def test_epic_integration_workspace_refreshes_from_advanced_target_branch(
    temp_db,
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    integration_path = tmp_path / "integration"
    repo.mkdir()
    _init_repo(repo)

    project = LocalProjectManager(temp_db).create("merge-project", repo_path=str(repo))
    task_manager = LocalTaskManager(temp_db)
    parent = task_manager.create_task(project_id=project.id, title="Parent", task_type="epic")
    integration_branch = _integration_branch(parent)

    _git(repo, "worktree", "add", "-b", integration_branch, str(integration_path), "main")
    worktrees = LocalWorktreeManager(temp_db)
    integration = worktrees.create(
        project_id=project.id,
        branch_name=integration_branch,
        worktree_path=str(integration_path),
        base_branch="main",
        task_id=parent.id,
        workspace_role="integration",
    )
    task_manager.artifacts.set_artifacts_atomic(
        parent.id,
        integration_branch=integration_branch,
        integration_workspace_id=integration.id,
        target_branch="main",
    )

    (repo / "after-child-merge.txt").write_text("landed on target\n")
    _git(repo, "add", "after-child-merge.txt")
    _git(repo, "commit", "-m", "land child merge on target")
    target_head = _git(repo, "rev-parse", "main")

    assert _git(integration_path, "rev-parse", "HEAD") != target_head

    ensure_epic_integration_workspaces(
        task_manager=task_manager,
        root_task=parent,
        backend="worktree",
        target_branch="main",
        project_id=project.id,
        services=None,
    )

    assert _git(integration_path, "rev-parse", "HEAD") == target_head
    assert (integration_path / "after-child-merge.txt").read_text() == "landed on target\n"


def test_epic_integration_workspace_adopts_pruned_metadata(
    temp_db,
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    integration_path = tmp_path / "integration"
    repo.mkdir()
    _init_repo(repo)

    project = LocalProjectManager(temp_db).create("merge-project", repo_path=str(repo))
    task_manager = LocalTaskManager(temp_db)
    parent = task_manager.create_task(project_id=project.id, title="Parent", task_type="epic")
    integration_branch = _integration_branch(parent)
    stale_worktree_id = "wt-pruned"

    _git(repo, "worktree", "add", "-b", integration_branch, str(integration_path), "main")
    task_manager.artifacts.set_artifacts_atomic(
        parent.id,
        integration_branch=integration_branch,
        integration_workspace_id=stale_worktree_id,
        target_branch="main",
    )

    ensure_epic_integration_workspaces(
        task_manager=task_manager,
        root_task=parent,
        backend="worktree",
        target_branch="main",
        project_id=project.id,
        services=None,
    )

    worktrees = LocalWorktreeManager(temp_db)
    adopted = worktrees.get_by_branch(project.id, integration_branch)
    parent_artifacts = task_manager.artifacts.get_artifacts(parent.id)

    assert adopted is not None
    assert adopted.id != stale_worktree_id
    assert adopted.worktree_path == str(integration_path)
    assert adopted.workspace_role == "integration"
    assert parent_artifacts.integration_workspace_id == adopted.id


def test_epic_integration_workspace_recreates_missing_path(
    monkeypatch: pytest.MonkeyPatch,
    temp_db,
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))

    project = LocalProjectManager(temp_db).create("merge-project", repo_path=str(repo))
    task_manager = LocalTaskManager(temp_db)
    parent = task_manager.create_task(project_id=project.id, title="Parent", task_type="epic")
    integration_branch = _integration_branch(parent)
    missing_path = tmp_path / "missing-integration"

    worktrees = LocalWorktreeManager(temp_db)
    stale = worktrees.create(
        project_id=project.id,
        branch_name=integration_branch,
        worktree_path=str(missing_path),
        base_branch="main",
        task_id=parent.id,
        workspace_role="integration",
    )
    task_manager.artifacts.set_artifacts_atomic(
        parent.id,
        integration_branch=integration_branch,
        integration_workspace_id=stale.id,
        target_branch="main",
    )

    ensure_epic_integration_workspaces(
        task_manager=task_manager,
        root_task=parent,
        backend="worktree",
        target_branch="main",
        project_id=project.id,
        services=None,
    )

    recreated = worktrees.get_by_branch(project.id, integration_branch)
    parent_artifacts = task_manager.artifacts.get_artifacts(parent.id)

    assert worktrees.get(stale.id) is None
    assert recreated is not None
    assert recreated.id != stale.id
    assert Path(recreated.worktree_path).is_dir()
    assert parent_artifacts.integration_workspace_id == recreated.id


def test_epic_integration_workspace_blocks_active_run_for_pruned_metadata(
    temp_db,
    tmp_path: Path,
) -> None:
    from gobby.storage.agents import LocalAgentRunManager

    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)

    project = LocalProjectManager(temp_db).create("merge-project", repo_path=str(repo))
    task_manager = LocalTaskManager(temp_db)
    parent = task_manager.create_task(project_id=project.id, title="Parent", task_type="epic")
    integration_branch = _integration_branch(parent)
    stale_worktree_id = "wt-active"
    temp_db.execute(
        "INSERT INTO sessions "
        "(id, external_id, machine_id, source, project_id, created_at, updated_at) "
        "VALUES (%s, %s, %s, %s, %s, NOW(), NOW())",
        ("parent-session", "ext-active", "machine-1", "codex", project.id),
    )
    run_manager = LocalAgentRunManager(temp_db)
    run = run_manager.create(
        parent_session_id="parent-session",
        provider="codex",
        prompt="review",
        agent_name="holistic-reviewer",
        task_id=parent.id,
        run_id="run-active-integration",
    )
    run_manager.update_runtime(run.id, worktree_id=stale_worktree_id)
    task_manager.artifacts.set_artifacts_atomic(
        parent.id,
        integration_branch=integration_branch,
        integration_workspace_id=stale_worktree_id,
        target_branch="main",
    )

    with pytest.raises(BuildWorkspaceError, match="active run run-active-integration"):
        ensure_epic_integration_workspaces(
            task_manager=task_manager,
            root_task=parent,
            backend="worktree",
            target_branch="main",
            project_id=project.id,
            services=None,
        )

    parent_artifacts = task_manager.artifacts.get_artifacts(parent.id)
    assert parent_artifacts.integration_workspace_id == stale_worktree_id


def test_epic_integration_workspace_merges_closed_descendant_commits(
    temp_db,
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    integration_path = tmp_path / "integration"
    repo.mkdir()
    _init_repo(repo)

    project = LocalProjectManager(temp_db).create("merge-project", repo_path=str(repo))
    task_manager = LocalTaskManager(temp_db)
    parent = task_manager.create_task(project_id=project.id, title="Parent", task_type="epic")
    leaf = task_manager.create_task(
        project_id=project.id,
        title="Leaf",
        parent_task_id=parent.id,
        category="code",
        task_type="task",
    )
    integration_branch = _integration_branch(parent)

    _git(repo, "worktree", "add", "-b", integration_branch, str(integration_path), "main")
    _git(repo, "checkout", "-b", "task/leaf")
    (repo / "feature.txt").write_text("feature\n")
    _git(repo, "add", "feature.txt")
    _git(repo, "commit", "-m", "feature")
    feature_sha = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "main")

    worktrees = LocalWorktreeManager(temp_db)
    integration = worktrees.create(
        project_id=project.id,
        branch_name=integration_branch,
        worktree_path=str(integration_path),
        base_branch="main",
        task_id=parent.id,
        workspace_role="integration",
    )
    task_manager.artifacts.set_artifacts_atomic(
        parent.id,
        integration_branch=integration_branch,
        integration_workspace_id=integration.id,
        target_branch="main",
    )
    task_manager.close_task_with_commit(leaf.id, feature_sha, force=True, cwd=repo)

    assert not _is_ancestor(integration_path, feature_sha)

    ensure_epic_integration_workspaces(
        task_manager=task_manager,
        root_task=parent,
        backend="worktree",
        target_branch="main",
        project_id=project.id,
        services=None,
        merge_closed_descendant_commits=True,
    )

    assert _is_ancestor(integration_path, feature_sha)
    assert (integration_path / "feature.txt").read_text() == "feature\n"


def test_epic_integration_workspace_prefers_closed_commit_over_stale_links(
    temp_db,
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    integration_path = tmp_path / "integration"
    repo.mkdir()
    _init_repo(repo)

    project = LocalProjectManager(temp_db).create("merge-project", repo_path=str(repo))
    task_manager = LocalTaskManager(temp_db)
    parent = task_manager.create_task(project_id=project.id, title="Parent", task_type="epic")
    leaf = task_manager.create_task(
        project_id=project.id,
        title="Leaf",
        parent_task_id=parent.id,
        category="code",
        task_type="task",
    )
    integration_branch = _integration_branch(parent)

    _git(repo, "worktree", "add", "-b", integration_branch, str(integration_path), "main")
    _git(repo, "checkout", "-b", "stale/leaf")
    (repo / "feature.txt").write_text("stale\n")
    _git(repo, "add", "feature.txt")
    _git(repo, "commit", "-m", "stale feature")
    stale_sha = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "main")
    _git(repo, "checkout", "-b", "task/leaf")
    (repo / "feature.txt").write_text("accepted\n")
    _git(repo, "add", "feature.txt")
    _git(repo, "commit", "-m", "accepted feature")
    accepted_sha = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "main")

    worktrees = LocalWorktreeManager(temp_db)
    integration = worktrees.create(
        project_id=project.id,
        branch_name=integration_branch,
        worktree_path=str(integration_path),
        base_branch="main",
        task_id=parent.id,
        workspace_role="integration",
    )
    task_manager.artifacts.set_artifacts_atomic(
        parent.id,
        integration_branch=integration_branch,
        integration_workspace_id=integration.id,
        target_branch="main",
    )
    task_manager.link_commit(leaf.id, stale_sha, cwd=repo)
    task_manager.close_task_with_commit(leaf.id, accepted_sha, force=True, cwd=repo)

    ensure_epic_integration_workspaces(
        task_manager=task_manager,
        root_task=parent,
        backend="worktree",
        target_branch="main",
        project_id=project.id,
        services=None,
        merge_closed_descendant_commits=True,
    )

    assert _is_ancestor(integration_path, accepted_sha)
    assert not _is_ancestor(integration_path, stale_sha)
    assert (integration_path / "feature.txt").read_text() == "accepted\n"


def test_epic_integration_workspace_skips_non_automation_planning_commits(
    temp_db,
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    integration_path = tmp_path / "integration"
    repo.mkdir()
    _init_repo(repo)

    project = LocalProjectManager(temp_db).create("merge-project", repo_path=str(repo))
    task_manager = LocalTaskManager(temp_db)
    parent = task_manager.create_task(project_id=project.id, title="Parent", task_type="epic")
    planning_child = task_manager.create_task(
        project_id=project.id,
        title="Interactive plan",
        parent_task_id=parent.id,
        category="planning",
        task_type="epic",
        labels=["interactive:planning"],
    )
    automated_leaf = task_manager.create_task(
        project_id=project.id,
        title="Automated leaf",
        parent_task_id=parent.id,
        category="code",
        task_type="task",
    )
    integration_branch = _integration_branch(parent)

    _git(repo, "worktree", "add", "-b", integration_branch, str(integration_path), "main")
    _git(integration_path, "config", "user.email", "test@example.com")
    _git(integration_path, "config", "user.name", "Test User")
    (integration_path / "README.md").write_text("current plan\n")
    _git(integration_path, "add", "README.md")
    _git(integration_path, "commit", "-m", "current integration plan")

    _git(repo, "checkout", "-b", "interactive/plan")
    (repo / "README.md").write_text("obsolete plan\n")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "obsolete interactive plan")
    stale_plan_sha = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "main")

    _git(repo, "checkout", "-b", "task/leaf")
    (repo / "feature.txt").write_text("feature\n")
    _git(repo, "add", "feature.txt")
    _git(repo, "commit", "-m", "feature")
    feature_sha = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "main")

    worktrees = LocalWorktreeManager(temp_db)
    integration = worktrees.create(
        project_id=project.id,
        branch_name=integration_branch,
        worktree_path=str(integration_path),
        base_branch="main",
        task_id=parent.id,
        workspace_role="integration",
    )
    task_manager.artifacts.set_artifacts_atomic(
        parent.id,
        integration_branch=integration_branch,
        integration_workspace_id=integration.id,
        target_branch="main",
    )
    task_manager.close_task_with_commit(planning_child.id, stale_plan_sha, force=True, cwd=repo)
    task_manager.update_task(automated_leaf.id, allow_automation=True)
    task_manager.close_task_with_commit(automated_leaf.id, feature_sha, force=True, cwd=repo)

    ensure_epic_integration_workspaces(
        task_manager=task_manager,
        root_task=parent,
        backend="worktree",
        target_branch="main",
        project_id=project.id,
        services=None,
        merge_closed_descendant_commits=True,
    )

    assert _is_ancestor(integration_path, feature_sha)
    assert not _is_ancestor(integration_path, stale_plan_sha)
    assert (integration_path / "README.md").read_text() == "current plan\n"
    assert (integration_path / "feature.txt").read_text() == "feature\n"


def test_epic_integration_workspace_refresh_aborts_timeout_merge(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "integration"
    workspace.mkdir()
    branch_name = "gobby/integration/phase"
    base_ref = "main"
    calls: list[tuple[str, ...]] = []

    def completed(
        args: list[str],
        *,
        returncode: int = 0,
        stdout: str = "",
        stderr: str = "",
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args, returncode, stdout, stderr)

    def fake_git(
        repo_path: Path,
        args: list[str],
        *,
        timeout: int,
        env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        assert repo_path == workspace
        calls.append(tuple(args))
        if args == ["status", "--porcelain"]:
            return completed(args)
        if args == ["branch", "--show-current"]:
            return completed(args, stdout=f"{branch_name}\n")
        if args == ["merge-base", "--is-ancestor", base_ref, "HEAD"]:
            return completed(args, returncode=1)
        if args == ["merge-base", "--is-ancestor", "HEAD", base_ref]:
            return completed(args, returncode=1)
        if args == ["merge", "--no-edit", base_ref]:
            assert env == {"GOBBY_MERGE": "1"}
            raise subprocess.TimeoutExpired(["git", *args], timeout)
        if args == ["merge", "--abort"]:
            return completed(args)
        raise AssertionError(f"unexpected git args: {args}")

    monkeypatch.setattr("gobby.build.workspace_git._git", fake_git)

    with pytest.raises(BuildWorkspaceError, match="git merge timed out"):
        _refresh_clean_git_dir(workspace, branch_name, base_ref)

    assert ("merge", "--abort") in calls


def test_epic_integration_workspace_clears_stale_task_worktree_artifacts(
    temp_db,
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    integration_path = tmp_path / "integration"
    stale_task_path = tmp_path / "stale-task"
    repo.mkdir()
    _init_repo(repo)

    project = LocalProjectManager(temp_db).create("merge-project", repo_path=str(repo))
    task_manager = LocalTaskManager(temp_db)
    parent = task_manager.create_task(project_id=project.id, title="Parent", task_type="epic")
    integration_branch = _integration_branch(parent)
    base_sha = _git(repo, "rev-parse", "main")

    _git(repo, "worktree", "add", "-b", integration_branch, str(integration_path), "main")
    _git(repo, "worktree", "add", "-b", "task/parent", str(stale_task_path), "main")
    worktrees = LocalWorktreeManager(temp_db)
    integration = worktrees.create(
        project_id=project.id,
        branch_name=integration_branch,
        worktree_path=str(integration_path),
        base_branch="main",
        task_id=parent.id,
        workspace_role="integration",
    )
    stale = worktrees.create(
        project_id=project.id,
        branch_name="task/parent",
        worktree_path=str(stale_task_path),
        base_branch="main",
        task_id=parent.id,
        workspace_role="task",
    )
    task_manager.artifacts.set_artifacts_atomic(
        parent.id,
        worktree_path=str(stale_task_path),
        worktree_id=stale.id,
        base_commit_sha=base_sha,
        integration_branch=integration_branch,
        integration_workspace_id=integration.id,
        target_branch="main",
    )

    (repo / "after-child-merge.txt").write_text("landed on target\n")
    _git(repo, "add", "after-child-merge.txt")
    _git(repo, "commit", "-m", "land child merge on target")
    target_head = _git(repo, "rev-parse", "main")

    ensure_epic_integration_workspaces(
        task_manager=task_manager,
        root_task=parent,
        backend="worktree",
        target_branch="main",
        project_id=project.id,
        services=None,
    )

    parent_artifacts = task_manager.artifacts.get_artifacts(parent.id)

    assert _git(integration_path, "rev-parse", "HEAD") == target_head
    assert parent_artifacts.integration_workspace_id == integration.id
    assert parent_artifacts.worktree_id is None
    assert parent_artifacts.worktree_path is None
    assert parent_artifacts.base_commit_sha is None


def test_epic_integration_workspace_promotes_existing_task_worktree(
    temp_db,
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    phase_path = tmp_path / "phase"
    repo.mkdir()
    _init_repo(repo)
    base_sha = _git(repo, "rev-parse", "main")

    _git(repo, "worktree", "add", "-b", "task/phase", str(phase_path), "main")
    _git(phase_path, "config", "user.email", "test@example.com")
    _git(phase_path, "config", "user.name", "Test User")
    (phase_path / "phase.txt").write_text("phase work\n")
    _git(phase_path, "add", "phase.txt")
    _git(phase_path, "commit", "-m", "phase work")
    phase_sha = _git(phase_path, "rev-parse", "HEAD")

    project = LocalProjectManager(temp_db).create("merge-project", repo_path=str(repo))
    task_manager = LocalTaskManager(temp_db)
    parent = task_manager.create_task(project_id=project.id, title="Parent", task_type="epic")
    worktrees = LocalWorktreeManager(temp_db)
    phase = worktrees.create(
        project_id=project.id,
        branch_name="task/phase",
        worktree_path=str(phase_path),
        base_branch="main",
        task_id=parent.id,
        workspace_role="task",
    )
    task_manager.artifacts.set_artifacts_atomic(
        parent.id,
        worktree_path=str(phase_path),
        worktree_id=phase.id,
        base_commit_sha=base_sha,
    )

    ensure_epic_integration_workspaces(
        task_manager=task_manager,
        root_task=parent,
        backend="worktree",
        target_branch="main",
        project_id=project.id,
        services=None,
    )

    parent_artifacts = task_manager.artifacts.get_artifacts(parent.id)
    promoted = worktrees.get(phase.id)

    assert parent_artifacts.target_branch == "main"
    assert parent_artifacts.integration_branch == "task/phase"
    assert parent_artifacts.integration_workspace_id == phase.id
    assert parent_artifacts.worktree_id is None
    assert parent_artifacts.worktree_path is None
    assert parent_artifacts.base_commit_sha is None
    assert promoted is not None
    assert promoted.workspace_role == "integration"
    assert _git(phase_path, "rev-parse", "HEAD") == phase_sha
    assert _git(repo, "branch", "--list", _integration_branch(parent)) == ""


def test_epic_integration_workspace_recovers_partially_promoted_worktree(
    temp_db,
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    phase_path = tmp_path / "phase"
    repo.mkdir()
    _init_repo(repo)
    base_sha = _git(repo, "rev-parse", "main")

    _git(repo, "worktree", "add", "-b", "task/phase", str(phase_path), "main")
    _git(phase_path, "config", "user.email", "test@example.com")
    _git(phase_path, "config", "user.name", "Test User")
    (phase_path / "phase.txt").write_text("phase work\n")
    _git(phase_path, "add", "phase.txt")
    _git(phase_path, "commit", "-m", "phase work")
    phase_sha = _git(phase_path, "rev-parse", "HEAD")

    project = LocalProjectManager(temp_db).create("merge-project", repo_path=str(repo))
    task_manager = LocalTaskManager(temp_db)
    parent = task_manager.create_task(project_id=project.id, title="Parent", task_type="epic")
    worktrees = LocalWorktreeManager(temp_db)
    phase = worktrees.create(
        project_id=project.id,
        branch_name="task/phase",
        worktree_path=str(phase_path),
        base_branch="main",
        task_id=parent.id,
        workspace_role="integration",
    )
    task_manager.artifacts.set_artifacts_atomic(
        parent.id,
        worktree_path=str(phase_path),
        worktree_id=phase.id,
        base_commit_sha=base_sha,
    )

    ensure_epic_integration_workspaces(
        task_manager=task_manager,
        root_task=parent,
        backend="worktree",
        target_branch="main",
        project_id=project.id,
        services=None,
    )

    parent_artifacts = task_manager.artifacts.get_artifacts(parent.id)
    stored = worktrees.get(phase.id)

    assert parent_artifacts.target_branch == "main"
    assert parent_artifacts.integration_branch == "task/phase"
    assert parent_artifacts.integration_workspace_id == phase.id
    assert parent_artifacts.worktree_id is None
    assert parent_artifacts.worktree_path is None
    assert stored is not None
    assert stored.workspace_role == "integration"
    assert _git(phase_path, "rev-parse", "HEAD") == phase_sha
    assert _git(repo, "branch", "--list", _integration_branch(parent)) == ""
