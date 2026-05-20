from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from gobby.build.workspaces import BuildWorkspaceError, _integration_branch
from gobby.dispatch.actions import MergeWorkspaceAction
from gobby.dispatch.workspace_merge import _non_gobby_status_lines, execute_merge_workspace
from gobby.storage.database import LocalDatabase
from gobby.storage.projects import LocalProjectManager
from gobby.storage.tasks import LocalTaskManager
from gobby.storage.worktrees import LocalWorktreeManager

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def _init_repo(path: Path) -> None:
    _git(path, "init", "-b", "main")
    _git(path, "config", "user.email", "test@example.com")
    _git(path, "config", "user.name", "Test User")
    (path / "README.md").write_text("initial\n")
    _git(path, "add", "README.md")
    _git(path, "commit", "-m", "initial")


def _assert_worktree_removed(
    worktrees: LocalWorktreeManager,
    worktree_id: str,
    worktree_path: Path,
) -> None:
    assert worktrees.get(worktree_id) is None
    assert not worktree_path.exists()


async def test_non_gobby_status_lines_ignores_gobby_paths_with_full_or_stripped_prefix() -> None:
    assert _non_gobby_status_lines(" M .gobby/tasks.jsonl\n") == []
    assert _non_gobby_status_lines("M .gobby/tasks.jsonl") == []
    assert _non_gobby_status_lines("R  .gobby/old.json -> .gobby/new.json") == []
    assert _non_gobby_status_lines("M src/gobby/app.py\n M .gobby/tasks.jsonl") == [
        "M src/gobby/app.py"
    ]


async def test_execute_merge_workspace_merges_worktree_and_completes_stage(
    temp_db: LocalDatabase,
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    integration_path = tmp_path / "integration"
    task_path = tmp_path / "task"
    repo.mkdir()
    _init_repo(repo)
    _git(repo, "worktree", "add", "-b", "integration/root", str(integration_path), "main")
    _git(repo, "worktree", "add", "-b", "task/leaf", str(task_path), "integration/root")
    _git(task_path, "config", "user.email", "test@example.com")
    _git(task_path, "config", "user.name", "Test User")
    (task_path / "feature.txt").write_text("feature\n")
    _git(task_path, "add", "feature.txt")
    _git(task_path, "commit", "-m", "feature")

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
    task_manager.initialize_task_manifest(leaf.id, stage_names=["merge"])
    task_manager.stage_states.start_stage(leaf.id, "merge", by_session_id="test")

    worktrees = LocalWorktreeManager(temp_db)
    worktrees.create(
        project_id=project.id,
        branch_name="integration/root",
        worktree_path=str(integration_path),
        base_branch="main",
        task_id=parent.id,
        workspace_role="integration",
    )
    source = worktrees.create(
        project_id=project.id,
        branch_name="task/leaf",
        worktree_path=str(task_path),
        base_branch="integration/root",
        task_id=leaf.id,
    )
    task_manager.artifacts.set_artifacts_atomic(
        leaf.id,
        worktree_path=str(task_path),
        worktree_id=source.id,
        base_commit_sha=_git(repo, "rev-parse", "main"),
        target_branch="integration/root",
    )

    merge_sha = await execute_merge_workspace(
        MergeWorkspaceAction(
            task_id=leaf.id,
            task_ref=f"#{leaf.seq_num}",
            backend="worktree",
            target_branch="integration/root",
            source_workspace_id=source.id,
        ),
        db=temp_db,
    )

    assert merge_sha == _git(integration_path, "rev-parse", "HEAD")
    assert (integration_path / "feature.txt").read_text() == "feature\n"
    assert task_manager.stage_states.get(leaf.id, "merge").state == "done"
    _assert_worktree_removed(worktrees, source.id, task_path)
    assert task_manager.artifacts.get_artifacts(leaf.id).worktree_id is None


async def test_execute_merge_workspace_lands_root_integration_worktree_on_local_branch(
    temp_db: LocalDatabase,
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    integration_path = tmp_path / "integration"
    task_path = tmp_path / "task"
    dirty_task_path = tmp_path / "dirty-task"
    repo.mkdir()
    _init_repo(repo)
    _git(repo, "worktree", "add", "-b", "gobby/integration/root", str(integration_path), "main")
    _git(repo, "worktree", "add", "-b", "task/stale-leaf", str(task_path), "main")
    _git(repo, "worktree", "add", "-b", "task/dirty-leaf", str(dirty_task_path), "main")
    (dirty_task_path / "dirty.txt").write_text("dirty\n")
    _git(integration_path, "config", "user.email", "test@example.com")
    _git(integration_path, "config", "user.name", "Test User")
    (integration_path / "feature.txt").write_text("feature\n")
    _git(integration_path, "add", "feature.txt")
    _git(integration_path, "commit", "-m", "feature")

    project = LocalProjectManager(temp_db).create("merge-project", repo_path=str(repo))
    task_manager = LocalTaskManager(temp_db)
    root = task_manager.create_task(project_id=project.id, title="Root", task_type="epic")
    leaf = task_manager.create_task(
        project_id=project.id,
        title="Stale leaf",
        parent_task_id=root.id,
        category="docs",
        task_type="task",
    )
    dirty_leaf = task_manager.create_task(
        project_id=project.id,
        title="Dirty leaf",
        parent_task_id=root.id,
        category="docs",
        task_type="task",
    )
    task_manager.initialize_task_manifest(root.id, stage_names=["merge"])
    task_manager.stage_states.start_stage(root.id, "merge", by_session_id="test")

    worktrees = LocalWorktreeManager(temp_db)
    integration = worktrees.create(
        project_id=project.id,
        branch_name="gobby/integration/root",
        worktree_path=str(integration_path),
        base_branch="main",
        task_id=root.id,
        workspace_role="integration",
    )
    source = worktrees.create(
        project_id=project.id,
        branch_name="task/stale-leaf",
        worktree_path=str(task_path),
        base_branch="main",
        task_id=leaf.id,
    )
    dirty_source = worktrees.create(
        project_id=project.id,
        branch_name="task/dirty-leaf",
        worktree_path=str(dirty_task_path),
        base_branch="main",
        task_id=dirty_leaf.id,
    )
    task_manager.artifacts.set_artifacts_atomic(
        root.id,
        target_branch="main",
        integration_branch="gobby/integration/root",
        integration_workspace_id=integration.id,
    )
    task_manager.artifacts.set_artifacts_atomic(
        leaf.id,
        worktree_path=str(task_path),
        worktree_id=source.id,
        base_commit_sha=_git(repo, "rev-parse", "main"),
        target_branch="gobby/integration/root",
    )
    task_manager.artifacts.set_artifacts_atomic(
        dirty_leaf.id,
        worktree_path=str(dirty_task_path),
        worktree_id=dirty_source.id,
        base_commit_sha=_git(repo, "rev-parse", "main"),
        target_branch="gobby/integration/root",
    )

    merge_sha = await execute_merge_workspace(
        MergeWorkspaceAction(
            task_id=root.id,
            task_ref=f"#{root.seq_num}",
            backend="worktree",
            target_branch="main",
            source_branch="gobby/integration/root",
            source_workspace_id=integration.id,
        ),
        db=temp_db,
    )

    stage = task_manager.stage_states.get(root.id, "merge")

    assert merge_sha == _git(repo, "rev-parse", "HEAD")
    assert merge_sha == _git(repo, "rev-parse", "main")
    assert (repo / "feature.txt").read_text() == "feature\n"
    assert stage is not None
    assert stage.state == "done"
    assert stage.completed_commit_sha == merge_sha
    assert stage.artifact_refs == {"integration_merge_sha": merge_sha}
    _assert_worktree_removed(worktrees, integration.id, integration_path)
    _assert_worktree_removed(worktrees, source.id, task_path)
    assert task_manager.artifacts.get_artifacts(root.id).integration_workspace_id is None
    assert task_manager.artifacts.get_artifacts(leaf.id).worktree_id is None
    assert worktrees.get(dirty_source.id) is not None
    assert dirty_task_path.exists()
    assert task_manager.artifacts.get_artifacts(dirty_leaf.id).worktree_id == dirty_source.id


async def test_execute_merge_workspace_lands_child_epic_integration_on_local_branch(
    temp_db: LocalDatabase,
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    integration_path = tmp_path / "phase-integration"
    repo.mkdir()
    _init_repo(repo)
    _git(repo, "worktree", "add", "-b", "gobby/integration/phase", str(integration_path), "main")
    _git(integration_path, "config", "user.email", "test@example.com")
    _git(integration_path, "config", "user.name", "Test User")
    (integration_path / "phase.txt").write_text("phase\n")
    _git(integration_path, "add", "phase.txt")
    _git(integration_path, "commit", "-m", "phase work")

    project = LocalProjectManager(temp_db).create("merge-project", repo_path=str(repo))
    task_manager = LocalTaskManager(temp_db)
    root = task_manager.create_task(project_id=project.id, title="Root", task_type="epic")
    phase = task_manager.create_task(
        project_id=project.id,
        title="Phase",
        parent_task_id=root.id,
        task_type="epic",
    )
    task_manager.initialize_task_manifest(phase.id, stage_names=["merge"])
    task_manager.stage_states.start_stage(phase.id, "merge", by_session_id="test")

    worktrees = LocalWorktreeManager(temp_db)
    integration = worktrees.create(
        project_id=project.id,
        branch_name="gobby/integration/phase",
        worktree_path=str(integration_path),
        base_branch="main",
        task_id=phase.id,
        workspace_role="integration",
    )
    task_manager.artifacts.set_artifacts_atomic(
        phase.id,
        target_branch="main",
        integration_branch="gobby/integration/phase",
        integration_workspace_id=integration.id,
    )
    (repo / ".gobby").mkdir()
    (repo / ".gobby" / "tasks.jsonl").write_text("sync artifact\n")

    merge_sha = await execute_merge_workspace(
        MergeWorkspaceAction(
            task_id=phase.id,
            task_ref=f"#{phase.seq_num}",
            backend="worktree",
            target_branch="main",
            source_branch="gobby/integration/phase",
            source_workspace_id=integration.id,
        ),
        db=temp_db,
    )

    stage = task_manager.stage_states.get(phase.id, "merge")

    assert merge_sha == _git(repo, "rev-parse", "HEAD")
    assert (repo / "phase.txt").read_text() == "phase\n"
    assert stage is not None
    assert stage.state == "done"
    assert stage.completed_commit_sha == merge_sha
    _assert_worktree_removed(worktrees, integration.id, integration_path)
    assert task_manager.artifacts.get_artifacts(phase.id).integration_workspace_id is None


async def test_execute_merge_workspace_adopts_missing_integration_worktree_metadata(
    temp_db: LocalDatabase,
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

    _git(repo, "worktree", "add", "-b", integration_branch, str(integration_path), "main")
    _git(repo, "worktree", "add", "-b", "task/leaf", str(task_path), integration_branch)
    (task_path / "feature.txt").write_text("feature\n")
    _git(task_path, "add", "feature.txt")
    _git(task_path, "commit", "-m", "feature")

    task_manager.initialize_task_manifest(leaf.id, stage_names=["merge"])
    task_manager.stage_states.start_stage(leaf.id, "merge", by_session_id="test")
    task_manager.artifacts.set_artifacts_atomic(
        parent.id,
        target_branch="main",
    )
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

    merge_sha = await execute_merge_workspace(
        MergeWorkspaceAction(
            task_id=leaf.id,
            task_ref=f"#{leaf.seq_num}",
            backend="worktree",
            target_branch=integration_branch,
            source_workspace_id=source.id,
        ),
        db=temp_db,
    )

    parent_artifacts = task_manager.artifacts.get_artifacts(parent.id)
    adopted = worktrees.get_by_branch(project.id, integration_branch)

    assert merge_sha == _git(integration_path, "rev-parse", "HEAD")
    assert adopted is not None
    assert adopted.workspace_role == "integration"
    assert adopted.task_id == parent.id
    assert adopted.worktree_path == str(integration_path)
    assert parent_artifacts.integration_workspace_id == adopted.id
    assert task_manager.stage_states.get(leaf.id, "merge").state == "done"
    _assert_worktree_removed(worktrees, source.id, task_path)
    assert task_manager.artifacts.get_artifacts(leaf.id).worktree_id is None


async def test_execute_merge_workspace_rejects_dirty_unmanaged_integration_worktree(
    temp_db: LocalDatabase,
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

    _git(repo, "worktree", "add", "-b", integration_branch, str(integration_path), "main")
    _git(repo, "worktree", "add", "-b", "task/leaf", str(task_path), integration_branch)
    (integration_path / "dirty.txt").write_text("dirty\n")
    (task_path / "feature.txt").write_text("feature\n")
    _git(task_path, "add", "feature.txt")
    _git(task_path, "commit", "-m", "feature")

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

    with pytest.raises(BuildWorkspaceError, match="integration workspace is dirty"):
        await execute_merge_workspace(
            MergeWorkspaceAction(
                task_id=leaf.id,
                task_ref=f"#{leaf.seq_num}",
                backend="worktree",
                target_branch=integration_branch,
                source_workspace_id=source.id,
            ),
            db=temp_db,
        )

    assert worktrees.get(source.id) is not None
    assert task_path.exists()


async def test_execute_merge_workspace_preserves_worktree_after_merge_conflict(
    temp_db: LocalDatabase,
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    integration_path = tmp_path / "integration"
    task_path = tmp_path / "task"
    repo.mkdir()
    _init_repo(repo)
    (repo / "conflict.txt").write_text("base\n")
    _git(repo, "add", "conflict.txt")
    _git(repo, "commit", "-m", "base conflict file")
    _git(repo, "worktree", "add", "-b", "integration/root", str(integration_path), "main")
    _git(repo, "worktree", "add", "-b", "task/leaf", str(task_path), "integration/root")
    _git(integration_path, "config", "user.email", "test@example.com")
    _git(integration_path, "config", "user.name", "Test User")
    _git(task_path, "config", "user.email", "test@example.com")
    _git(task_path, "config", "user.name", "Test User")
    (integration_path / "conflict.txt").write_text("integration\n")
    _git(integration_path, "add", "conflict.txt")
    _git(integration_path, "commit", "-m", "integration change")
    (task_path / "conflict.txt").write_text("task\n")
    _git(task_path, "add", "conflict.txt")
    _git(task_path, "commit", "-m", "task change")

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
    task_manager.initialize_task_manifest(leaf.id, stage_names=["merge"])
    task_manager.stage_states.start_stage(leaf.id, "merge", by_session_id="test")

    worktrees = LocalWorktreeManager(temp_db)
    worktrees.create(
        project_id=project.id,
        branch_name="integration/root",
        worktree_path=str(integration_path),
        base_branch="main",
        task_id=parent.id,
        workspace_role="integration",
    )
    source = worktrees.create(
        project_id=project.id,
        branch_name="task/leaf",
        worktree_path=str(task_path),
        base_branch="integration/root",
        task_id=leaf.id,
    )
    task_manager.artifacts.set_artifacts_atomic(
        leaf.id,
        worktree_path=str(task_path),
        worktree_id=source.id,
        base_commit_sha=_git(repo, "rev-parse", "main"),
        target_branch="integration/root",
    )

    merge_sha = await execute_merge_workspace(
        MergeWorkspaceAction(
            task_id=leaf.id,
            task_ref=f"#{leaf.seq_num}",
            backend="worktree",
            target_branch="integration/root",
            source_workspace_id=source.id,
        ),
        db=temp_db,
    )

    stage = task_manager.stage_states.get(leaf.id, "merge")
    assert merge_sha is None
    assert stage is not None
    assert stage.state == "ready"
    assert worktrees.get(source.id) is not None
    assert task_path.exists()
    assert task_manager.artifacts.get_artifacts(leaf.id).worktree_id == source.id


async def test_execute_merge_workspace_resolves_worktree_local_project_metadata(
    temp_db: LocalDatabase,
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    integration_path = tmp_path / "integration"
    task_path = tmp_path / "task"
    repo.mkdir()
    _init_repo(repo)
    (repo / ".gobby").mkdir()
    project_json = '{\n  "id": "proj-1",\n  "name": "merge-project"\n}\n'
    (repo / ".gobby" / "project.json").write_text(project_json)
    _git(repo, "add", ".gobby/project.json")
    _git(repo, "commit", "-m", "add project metadata")
    _git(repo, "worktree", "add", "-b", "integration/root", str(integration_path), "main")
    _git(repo, "worktree", "add", "-b", "task/leaf", str(task_path), "integration/root")
    _git(integration_path, "config", "user.email", "test@example.com")
    _git(integration_path, "config", "user.name", "Test User")
    _git(task_path, "config", "user.email", "test@example.com")
    _git(task_path, "config", "user.name", "Test User")

    integration_project_json = (
        '{\n  "id": "proj-1",\n  "name": "merge-project",\n  "parent_project_path": "/repo"\n}\n'
    )
    (integration_path / ".gobby" / "project.json").write_text(integration_project_json)
    _git(integration_path, "add", ".gobby/project.json")
    _git(integration_path, "commit", "-m", "record integration project path")

    task_project_json = (
        '{\n  "id": "proj-1",\n  "name": "merge-project",\n  "parent_project_id": "proj-1"\n}\n'
    )
    (task_path / ".gobby" / "project.json").write_text(task_project_json)
    (task_path / "feature.txt").write_text("feature\n")
    _git(task_path, "add", ".gobby/project.json", "feature.txt")
    _git(task_path, "commit", "-m", "feature with local project metadata")

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
    task_manager.initialize_task_manifest(leaf.id, stage_names=["merge"])
    task_manager.stage_states.start_stage(leaf.id, "merge", by_session_id="test")

    worktrees = LocalWorktreeManager(temp_db)
    worktrees.create(
        project_id=project.id,
        branch_name="integration/root",
        worktree_path=str(integration_path),
        base_branch="main",
        task_id=parent.id,
        workspace_role="integration",
    )
    source = worktrees.create(
        project_id=project.id,
        branch_name="task/leaf",
        worktree_path=str(task_path),
        base_branch="integration/root",
        task_id=leaf.id,
    )
    task_manager.artifacts.set_artifacts_atomic(
        leaf.id,
        worktree_path=str(task_path),
        worktree_id=source.id,
        base_commit_sha=_git(repo, "rev-parse", "main"),
        target_branch="integration/root",
    )

    merge_sha = await execute_merge_workspace(
        MergeWorkspaceAction(
            task_id=leaf.id,
            task_ref=f"#{leaf.seq_num}",
            backend="worktree",
            target_branch="integration/root",
            source_workspace_id=source.id,
        ),
        db=temp_db,
    )

    assert merge_sha == _git(integration_path, "rev-parse", "HEAD")
    assert (integration_path / "feature.txt").read_text() == "feature\n"
    assert (integration_path / ".gobby" / "project.json").read_text() == integration_project_json
    assert task_manager.stage_states.get(leaf.id, "merge").state == "done"
    _assert_worktree_removed(worktrees, source.id, task_path)
    assert task_manager.artifacts.get_artifacts(leaf.id).worktree_id is None


async def test_execute_merge_workspace_resolves_docs_guides_readme_row_conflict(
    temp_db: LocalDatabase,
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    integration_path = tmp_path / "integration"
    task_path = tmp_path / "task"
    repo.mkdir()
    _init_repo(repo)
    readme = repo / "docs" / "guides" / "README.md"
    readme.parent.mkdir(parents=True)
    readme.write_text(
        "# Gobby Guides\n\n"
        "Documentation guides for using Gobby's features.\n\n"
        "## Core Features\n\n"
        "| Guide | Description |\n"
        "|-------|-------------|\n"
        "| [search.md](search.md) | Unified search with TF-IDF, embeddings, and hybrid modes |\n"
        "| [tasks.md](tasks.md) | Task management with dependencies and validation |\n"
    )
    _git(repo, "add", "docs/guides/README.md")
    _git(repo, "commit", "-m", "add guides index")
    _git(repo, "worktree", "add", "-b", "integration/root", str(integration_path), "main")
    _git(repo, "worktree", "add", "-b", "task/leaf", str(task_path), "integration/root")
    _git(integration_path, "config", "user.email", "test@example.com")
    _git(integration_path, "config", "user.name", "Test User")
    _git(task_path, "config", "user.email", "test@example.com")
    _git(task_path, "config", "user.name", "Test User")

    integration_readme = (
        "# Gobby Guides\n\n"
        "Task-focused documentation for Gobby users, operators, and contributors.\n\n"
        "## Core Features\n\n"
        "| Guide | Description |\n"
        "|-------|-------------|\n"
        "| [search.md](./search.md) | Search across memories, tasks, and code content |\n"
        "| [tasks.md](./tasks.md) | Task management with dependencies, expansion, and git sync |\n"
        "\n_Last verified: 2026-05-06_\n"
    )
    (integration_path / "docs" / "guides" / "README.md").write_text(integration_readme)
    _git(integration_path, "add", "docs/guides/README.md")
    _git(integration_path, "commit", "-m", "refresh guide index structure")

    task_readme = (
        "# Gobby Guides\n\n"
        "Documentation guides for using Gobby's features.\n\n"
        "## Core Features\n\n"
        "| Guide | Description |\n"
        "|-------|-------------|\n"
        "| [search.md](search.md) | Domain search for tasks, memories, skills, and MCP tools |\n"
        "| [tasks.md](tasks.md) | Task management with dependencies and validation |\n"
    )
    (task_path / "docs" / "guides" / "README.md").write_text(task_readme)
    (task_path / "docs" / "guides" / "search.md").write_text("search\n")
    _git(task_path, "add", "docs/guides/README.md", "docs/guides/search.md")
    _git(task_path, "commit", "-m", "refresh search guide")

    project = LocalProjectManager(temp_db).create("merge-project", repo_path=str(repo))
    task_manager = LocalTaskManager(temp_db)
    parent = task_manager.create_task(project_id=project.id, title="Parent", task_type="epic")
    leaf = task_manager.create_task(
        project_id=project.id,
        title="Leaf",
        parent_task_id=parent.id,
        category="docs",
        task_type="task",
    )
    task_manager.initialize_task_manifest(leaf.id, stage_names=["merge"])
    task_manager.stage_states.start_stage(leaf.id, "merge", by_session_id="test")

    worktrees = LocalWorktreeManager(temp_db)
    worktrees.create(
        project_id=project.id,
        branch_name="integration/root",
        worktree_path=str(integration_path),
        base_branch="main",
        task_id=parent.id,
        workspace_role="integration",
    )
    source = worktrees.create(
        project_id=project.id,
        branch_name="task/leaf",
        worktree_path=str(task_path),
        base_branch="integration/root",
        task_id=leaf.id,
    )
    task_manager.artifacts.set_artifacts_atomic(
        leaf.id,
        worktree_path=str(task_path),
        worktree_id=source.id,
        base_commit_sha=_git(repo, "rev-parse", "main"),
        target_branch="integration/root",
    )

    merge_sha = await execute_merge_workspace(
        MergeWorkspaceAction(
            task_id=leaf.id,
            task_ref=f"#{leaf.seq_num}",
            backend="worktree",
            target_branch="integration/root",
            source_workspace_id=source.id,
        ),
        db=temp_db,
    )

    expected_readme = integration_readme.replace(
        "Search across memories, tasks, and code content",
        "Domain search for tasks, memories, skills, and MCP tools",
    )
    assert merge_sha == _git(integration_path, "rev-parse", "HEAD")
    assert (integration_path / "docs" / "guides" / "README.md").read_text() == expected_readme
    assert (integration_path / "docs" / "guides" / "search.md").read_text() == "search\n"
    assert task_manager.stage_states.get(leaf.id, "merge").state == "done"
    _assert_worktree_removed(worktrees, source.id, task_path)
    assert task_manager.artifacts.get_artifacts(leaf.id).worktree_id is None


async def test_execute_merge_workspace_resolves_represented_docs_guides_readme_quick_link(
    temp_db: LocalDatabase,
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    integration_path = tmp_path / "integration"
    task_path = tmp_path / "task"
    repo.mkdir()
    _init_repo(repo)
    readme = repo / "docs" / "guides" / "README.md"
    readme.parent.mkdir(parents=True)
    base_readme = (
        "# Gobby Guides\n\n"
        "Documentation guides for using Gobby's features.\n\n"
        "## Quick Links\n\n"
        '- **Create a task**: `gobby tasks create "Title"` or `create_task` MCP tool\n'
        "- **Session handoff**: `gobby sessions create-handoff` or `create_handoff` MCP tool\n"
    )
    readme.write_text(base_readme)
    _git(repo, "add", "docs/guides/README.md")
    _git(repo, "commit", "-m", "add guides index")
    _git(repo, "worktree", "add", "-b", "integration/root", str(integration_path), "main")
    _git(repo, "worktree", "add", "-b", "task/leaf", str(task_path), "integration/root")
    _git(integration_path, "config", "user.email", "test@example.com")
    _git(integration_path, "config", "user.name", "Test User")
    _git(task_path, "config", "user.email", "test@example.com")
    _git(task_path, "config", "user.name", "Test User")

    integration_readme = (
        "# Gobby Guides\n\n"
        "Task-focused documentation for Gobby users, operators, and contributors.\n\n"
        "## Quick Links\n\n"
        '- **Create a task**: `gobby tasks create "Title"` or '
        '`create_task(title="Title", category="docs")`\n'
        "- **Session handoff**: `gobby sessions create-handoff` or "
        "`set_handoff_context` MCP tool\n"
        "\n_Last verified: 2026-05-06_\n"
    )
    (integration_path / "docs" / "guides" / "README.md").write_text(integration_readme)
    _git(integration_path, "add", "docs/guides/README.md")
    _git(integration_path, "commit", "-m", "refresh guide index quick links")

    task_readme = base_readme.replace("create_handoff", "set_handoff_context")
    (task_path / "docs" / "guides" / "README.md").write_text(task_readme)
    (task_path / "docs" / "guides" / "mcp-tools.md").write_text("mcp tools\n")
    _git(task_path, "add", "docs/guides/README.md", "docs/guides/mcp-tools.md")
    _git(task_path, "commit", "-m", "refresh mcp tools guide")

    project = LocalProjectManager(temp_db).create("merge-project", repo_path=str(repo))
    task_manager = LocalTaskManager(temp_db)
    parent = task_manager.create_task(project_id=project.id, title="Parent", task_type="epic")
    leaf = task_manager.create_task(
        project_id=project.id,
        title="Leaf",
        parent_task_id=parent.id,
        category="docs",
        task_type="task",
    )
    task_manager.initialize_task_manifest(leaf.id, stage_names=["merge"])
    task_manager.stage_states.start_stage(leaf.id, "merge", by_session_id="test")

    worktrees = LocalWorktreeManager(temp_db)
    worktrees.create(
        project_id=project.id,
        branch_name="integration/root",
        worktree_path=str(integration_path),
        base_branch="main",
        task_id=parent.id,
        workspace_role="integration",
    )
    source = worktrees.create(
        project_id=project.id,
        branch_name="task/leaf",
        worktree_path=str(task_path),
        base_branch="integration/root",
        task_id=leaf.id,
    )
    task_manager.artifacts.set_artifacts_atomic(
        leaf.id,
        worktree_path=str(task_path),
        worktree_id=source.id,
        base_commit_sha=_git(repo, "rev-parse", "main"),
        target_branch="integration/root",
    )

    merge_sha = await execute_merge_workspace(
        MergeWorkspaceAction(
            task_id=leaf.id,
            task_ref=f"#{leaf.seq_num}",
            backend="worktree",
            target_branch="integration/root",
            source_workspace_id=source.id,
        ),
        db=temp_db,
    )

    assert merge_sha == _git(integration_path, "rev-parse", "HEAD")
    assert (integration_path / "docs" / "guides" / "README.md").read_text() == integration_readme
    assert (integration_path / "docs" / "guides" / "mcp-tools.md").read_text() == "mcp tools\n"
    assert task_manager.stage_states.get(leaf.id, "merge").state == "done"
    _assert_worktree_removed(worktrees, source.id, task_path)
    assert task_manager.artifacts.get_artifacts(leaf.id).worktree_id is None
