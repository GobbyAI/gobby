"""Tests for task-scoped build lifecycle controls."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from gobby.storage.agents import LocalAgentRunManager
from gobby.storage.clones import LocalCloneManager
from gobby.storage.projects import LocalProjectManager
from gobby.storage.sessions import SessionManager
from gobby.storage.tasks import LocalTaskManager, Task

pytestmark = pytest.mark.unit


def _set_project_repo(temp_db, project_id: str, tmp_path: Path) -> Path:
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    LocalProjectManager(temp_db).update(project_id, repo_path=str(repo_path))
    return repo_path


def _tree(task_manager: LocalTaskManager, project_id: str) -> tuple[Task, list[Task]]:
    epic = task_manager.create_task(
        project_id=project_id,
        title="Lifecycle controls",
        task_type="epic",
        category="planning",
    )
    child = task_manager.create_task(
        project_id=project_id,
        title="Child code task",
        parent_task_id=epic.id,
        task_type="task",
        category="code",
    )
    leaf = task_manager.create_task(
        project_id=project_id,
        title="Leaf docs task",
        parent_task_id=epic.id,
        task_type="task",
        category="docs",
    )
    return epic, [child, leaf]


@pytest.mark.asyncio
async def test_stop_disables_leaf_automation_and_cancels_active_agent(
    temp_db,
    sample_project,
) -> None:
    from gobby.build.controls import build_stop_target

    task_manager = LocalTaskManager(temp_db)
    task = task_manager.create_task(
        project_id=sample_project["id"],
        title="Runaway build",
        category="code",
        task_type="task",
    )
    task_manager.update_task(task.id, allow_automation=True, unattended=True)

    run_manager = LocalAgentRunManager(temp_db)
    parent_session = SessionManager(temp_db).register(
        external_id="stop-target-parent",
        machine_id="machine-1",
        source="codex",
        project_id=sample_project["id"],
    )
    run = run_manager.create(
        parent_session_id=parent_session.id,
        provider="codex",
        prompt="work",
        task_id=task.id,
        run_id="run-stop-target",
    )
    run_manager.start(run.id)

    with patch("gobby.build.controls.kill_agent", new=AsyncMock(return_value={"success": True})):
        result = await build_stop_target(
            f"#{task.seq_num}",
            db=temp_db,
            project_id=sample_project["id"],
        )

    updated = task_manager.get_task(task.id)
    cancelled = run_manager.get(run.id)
    assert result.action == "stop"
    assert result.automation_updated == 1
    assert updated.allow_automation is False
    assert updated.unattended is False
    assert cancelled is not None
    assert cancelled.status == "cancelled"


@pytest.mark.asyncio
async def test_stop_clears_runtime_claim_and_resets_current_stage(
    temp_db,
    sample_project,
) -> None:
    from gobby.build.controls import build_stop_target
    from gobby.storage.tasks._dispatch_mutex import TaskDispatchMutexManager
    from tests.storage.tasks._stage_test_helpers import (
        initialize_manifest,
        lifecycle_events,
        set_stage_state,
        spec,
        stage_row,
    )

    task_manager = LocalTaskManager(temp_db)
    task = task_manager.create_task(
        project_id=sample_project["id"],
        title="Phantom build",
        category="code",
        task_type="task",
    )
    task_manager.update_task(task.id, allow_automation=True, unattended=True)
    initialize_manifest(temp_db, task.id, [spec("development", 0)])
    set_stage_state(temp_db, task.id, "development", "in_progress", work_attempt_count=1)

    sessions = SessionManager(temp_db)
    parent = sessions.register(
        external_id="phantom-parent",
        machine_id="machine-1",
        source="codex",
        project_id=sample_project["id"],
    )
    child = sessions.register(
        external_id="phantom-child",
        machine_id="machine-1",
        source="codex",
        project_id=sample_project["id"],
        parent_session_id=parent.id,
    )
    task_manager.claim_task(task.id, session_id=child.id)
    run_manager = LocalAgentRunManager(temp_db)
    run = run_manager.create(
        parent_session_id=parent.id,
        child_session_id=child.id,
        provider="codex",
        prompt="work",
        task_id=task.id,
        run_id="run-phantom",
    )
    run_manager.start(run.id)
    TaskDispatchMutexManager(temp_db).acquire_mutex(
        task.id,
        holder="dispatcher",
        kind="spawn_agent",
        ttl_seconds=300,
        run_id=run.id,
    )

    with patch("gobby.build.controls.kill_agent", new=AsyncMock(return_value={"success": True})):
        result = await build_stop_target(
            f"#{task.seq_num}",
            db=temp_db,
            project_id=sample_project["id"],
        )

    updated = task_manager.get_task(task.id)
    assert updated.claimed_by_session_id is None
    assert updated.assignee is None
    assert TaskDispatchMutexManager(temp_db).get_mutex(task.id) is None
    assert stage_row(temp_db, task.id, "development")["state"] == "ready"
    assert result.claims_released == 1
    assert result.mutexes_cleared >= 1
    assert result.stages_reset == 1
    assert lifecycle_events(temp_db, task.id)[-1]["reason"] == "build_stop"


@pytest.mark.asyncio
async def test_stop_prevents_dispatcher_respawn_on_next_heartbeat(
    monkeypatch: pytest.MonkeyPatch,
    temp_db,
    sample_project,
) -> None:
    from gobby.build.controls import build_stop_target
    from gobby.dispatch import dispatcher
    from tests.storage.tasks._stage_test_helpers import initialize_manifest, set_stage_state, spec

    task_manager = LocalTaskManager(temp_db)
    task = task_manager.create_task(
        project_id=sample_project["id"],
        title="Ready dispatch task",
        category="code",
        task_type="task",
    )
    task_manager.update_task(
        task.id,
        allow_automation=True,
        assigned_agent="backend-developer",
        isolation="none",
    )
    initialize_manifest(temp_db, task.id, [spec("development", 0)])
    set_stage_state(temp_db, task.id, "development", "ready")

    await build_stop_target(f"#{task.seq_num}", db=temp_db, project_id=sample_project["id"])

    def fail_evaluate(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("stopped task should not reach dispatch evaluation")

    monkeypatch.setattr(dispatcher.dispatch_rules, "evaluate", fail_evaluate)

    result = await dispatcher.run_heartbeat(db=temp_db, project_id=sample_project["id"])

    assert result.scanned == 0
    assert result.executed == 0


@pytest.mark.asyncio
async def test_resume_epic_sets_subtree_automation_and_kicks_dispatcher(
    temp_db,
    sample_project,
) -> None:
    from gobby.build.controls import build_resume_target

    task_manager = LocalTaskManager(temp_db)
    epic, descendants = _tree(task_manager, sample_project["id"])
    for task in [epic, *descendants]:
        task_manager.update_task(task.id, allow_automation=False)

    with patch(
        "gobby.build.controls._kick_dispatcher_tick",
        new=AsyncMock(return_value=object()),
    ):
        result = await build_resume_target(
            f"#{epic.seq_num}",
            db=temp_db,
            project_id=sample_project["id"],
        )

    assert result.action == "resume"
    assert result.automation_updated == 3
    for task in [epic, *descendants]:
        assert task_manager.get_task(task.id).allow_automation is True


@pytest.mark.asyncio
async def test_resume_clears_orphan_no_run_dispatch_mutex(
    temp_db,
    sample_project,
) -> None:
    from gobby.build.controls import build_resume_target
    from gobby.storage.tasks._dispatch_mutex import TaskDispatchMutexManager

    task_manager = LocalTaskManager(temp_db)
    task = task_manager.create_task(
        project_id=sample_project["id"],
        title="Orphan dispatch mutex",
        category="code",
        task_type="task",
    )
    storage = TaskDispatchMutexManager(temp_db)
    acquired_at = datetime.now(UTC) - timedelta(seconds=60)
    assert storage.acquire_mutex(
        task.id,
        holder="dispatcher",
        kind="heartbeat",
        ttl_seconds=600,
        now=acquired_at,
    )

    with patch(
        "gobby.build.controls._kick_dispatcher_tick",
        new=AsyncMock(return_value=object()),
    ):
        result = await build_resume_target(
            f"#{task.seq_num}",
            db=temp_db,
            project_id=sample_project["id"],
        )

    assert storage.get_mutex(task.id) is None
    assert result.mutexes_cleared == 1


@pytest.mark.asyncio
async def test_resume_preserves_fresh_no_run_dispatch_mutex(
    temp_db,
    sample_project,
) -> None:
    from gobby.build.controls import build_resume_target
    from gobby.storage.tasks._dispatch_mutex import TaskDispatchMutexManager

    task_manager = LocalTaskManager(temp_db)
    task = task_manager.create_task(
        project_id=sample_project["id"],
        title="Fresh dispatch mutex",
        category="code",
        task_type="task",
    )
    storage = TaskDispatchMutexManager(temp_db)
    assert storage.acquire_mutex(
        task.id,
        holder="dispatcher",
        kind="heartbeat",
        ttl_seconds=600,
        now=datetime.now(UTC),
    )

    with patch(
        "gobby.build.controls._kick_dispatcher_tick",
        new=AsyncMock(return_value=object()),
    ):
        result = await build_resume_target(
            f"#{task.seq_num}",
            db=temp_db,
            project_id=sample_project["id"],
        )

    assert storage.get_mutex(task.id) is not None
    assert result.mutexes_cleared == 0


@pytest.mark.asyncio
async def test_clean_dry_run_reports_blockers_and_artifacts(
    temp_db,
    sample_project,
    tmp_path: Path,
) -> None:
    from gobby.build.controls import build_clean_target
    from gobby.storage.tasks import TaskArtifactManager

    _set_project_repo(temp_db, sample_project["id"], tmp_path)
    task_manager = LocalTaskManager(temp_db)
    task = task_manager.create_task(
        project_id=sample_project["id"],
        title="Failed clone task",
        category="code",
        task_type="task",
    )
    task_manager.update_task(task.id, allow_automation=True)
    clone_path = tmp_path / "clone"
    clone_path.mkdir()
    TaskArtifactManager(temp_db).set_artifacts_atomic(
        task.id,
        clone_path=str(clone_path),
        clone_id="clone-dry-run",
        base_commit_sha="abc123",
    )

    result = await build_clean_target(
        f"#{task.seq_num}",
        db=temp_db,
        project_id=sample_project["id"],
        dry_run=True,
    )

    assert result.dry_run is True
    assert result.artifacts[0].path == str(clone_path)
    assert result.blocked_reasons
    assert clone_path.exists()


@pytest.mark.asyncio
async def test_clean_force_deletes_clone_and_clears_artifact_pair(
    temp_db,
    sample_project,
    tmp_path: Path,
) -> None:
    from gobby.build.controls import build_clean_target
    from gobby.storage.tasks import TaskArtifactManager

    _set_project_repo(temp_db, sample_project["id"], tmp_path)
    task_manager = LocalTaskManager(temp_db)
    task = task_manager.create_task(
        project_id=sample_project["id"],
        title="Failed clone cleanup",
        category="code",
        task_type="task",
    )
    task_manager.update_task(task.id, allow_automation=False)
    clone_path = tmp_path / "clone"
    clone_path.mkdir()
    clone = LocalCloneManager(temp_db).create(
        project_id=sample_project["id"],
        branch_name="task-1-failed",
        clone_path=str(clone_path),
        task_id=task.id,
    )
    TaskArtifactManager(temp_db).set_artifacts_atomic(
        task.id,
        clone_path=str(clone_path),
        clone_id=clone.id,
        base_commit_sha="abc123",
    )

    result = await build_clean_target(
        f"#{task.seq_num}",
        db=temp_db,
        project_id=sample_project["id"],
        force=True,
        yes=True,
    )

    artifacts = task_manager.artifacts.get_artifacts(task.id)
    assert result.artifacts[0].deleted is True
    assert not clone_path.exists()
    assert LocalCloneManager(temp_db).get(clone.id) is None
    assert artifacts.clone_path is None
    assert artifacts.clone_id is None


def test_successful_merge_cleanup_defers_active_agent_worktree(
    monkeypatch: pytest.MonkeyPatch,
    temp_db,
    sample_project,
    tmp_path: Path,
) -> None:
    from gobby.build import controls
    from gobby.storage.tasks import TaskArtifactManager
    from gobby.storage.worktrees import LocalWorktreeManager

    monkeypatch.setattr(controls, "LocalTaskManager", LocalTaskManager)
    _set_project_repo(temp_db, sample_project["id"], tmp_path)
    task_manager = LocalTaskManager(temp_db)
    task = task_manager.create_task(
        project_id=sample_project["id"],
        title="Merged while agent still exits",
        category="code",
        task_type="task",
    )
    worktree_path = tmp_path / "active-worktree"
    worktree_path.mkdir()
    worktree = LocalWorktreeManager(temp_db).create(
        project_id=sample_project["id"],
        branch_name="task-active-worktree",
        worktree_path=str(worktree_path),
        base_branch="0.4.7",
        task_id=task.id,
    )
    TaskArtifactManager(temp_db).set_artifacts_atomic(
        task.id,
        worktree_path=str(worktree_path),
        worktree_id=worktree.id,
        base_commit_sha="abc123",
    )

    sessions = SessionManager(temp_db)
    parent = sessions.register(
        external_id="merge-parent",
        machine_id="machine-1",
        source="claude",
        project_id=sample_project["id"],
    )
    child = sessions.register(
        external_id="merge-child",
        machine_id="machine-1",
        source="claude",
        project_id=sample_project["id"],
        parent_session_id=parent.id,
    )
    run_manager = LocalAgentRunManager(temp_db)
    run = run_manager.create(
        parent_session_id=parent.id,
        child_session_id=child.id,
        provider="claude",
        prompt="merge",
        task_id=task.id,
        run_id="run-active-merge-cleanup",
    )
    run_manager.start(run.id)
    run_manager.update_runtime(run.id, worktree_id=worktree.id)

    class ExplodingWorktreeGitManager:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        def delete_worktree(self, *_args: object, **_kwargs: object) -> None:
            raise AssertionError("active agent worktree must not be deleted")

    monkeypatch.setattr(controls, "WorktreeGitManager", ExplodingWorktreeGitManager)

    def fail_branch_cleanup(*_args: object, **_kwargs: object) -> tuple[int, list[str]]:
        raise AssertionError("branch cleanup must wait while an agent owns the worktree")

    monkeypatch.setattr(controls, "delete_orphan_build_branches", fail_branch_cleanup)

    artifacts = controls.cleanup_successful_merge_artifacts(
        temp_db,
        task.id,
        project_id=sample_project["id"],
    )

    assert len(artifacts) == 1
    assert artifacts[0].deferred is True
    assert artifacts[0].deleted is False
    assert worktree_path.exists()
    assert LocalWorktreeManager(temp_db).get(worktree.id) is not None
    stored = TaskArtifactManager(temp_db).get_artifacts(task.id)
    assert stored.worktree_id == worktree.id
    assert stored.worktree_path == str(worktree_path)


def test_successful_merge_cleanup_deletes_inactive_worktree(
    monkeypatch: pytest.MonkeyPatch,
    temp_db,
    sample_project,
    tmp_path: Path,
) -> None:
    from gobby.build import controls
    from gobby.storage.tasks import TaskArtifactManager
    from gobby.storage.worktrees import LocalWorktreeManager

    monkeypatch.setattr(controls, "LocalTaskManager", LocalTaskManager)
    _set_project_repo(temp_db, sample_project["id"], tmp_path)
    task_manager = LocalTaskManager(temp_db)
    task = task_manager.create_task(
        project_id=sample_project["id"],
        title="Merged after agent exit",
        category="code",
        task_type="task",
    )
    worktree_path = tmp_path / "inactive-worktree"
    worktree_path.mkdir()
    worktree = LocalWorktreeManager(temp_db).create(
        project_id=sample_project["id"],
        branch_name="task-inactive-worktree",
        worktree_path=str(worktree_path),
        base_branch="0.4.7",
        task_id=task.id,
    )
    TaskArtifactManager(temp_db).set_artifacts_atomic(
        task.id,
        worktree_path=str(worktree_path),
        worktree_id=worktree.id,
        base_commit_sha="abc123",
    )

    delete_calls: list[tuple[Path, bool]] = []

    class DeletingWorktreeGitManager:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        def delete_worktree(self, path: Path, *, force: bool = False) -> SimpleNamespace:
            delete_calls.append((Path(path), force))
            Path(path).rmdir()
            return SimpleNamespace(success=True, error=None, message="deleted")

    monkeypatch.setattr(controls, "WorktreeGitManager", DeletingWorktreeGitManager)
    monkeypatch.setattr(controls, "delete_orphan_build_branches", lambda *_args: (0, []))

    artifacts = controls.cleanup_successful_merge_artifacts(
        temp_db,
        task.id,
        project_id=sample_project["id"],
    )

    assert len(artifacts) == 1
    assert artifacts[0].deleted is True
    assert artifacts[0].deferred is False
    assert delete_calls == [(worktree_path, True)]
    assert not worktree_path.exists()
    assert LocalWorktreeManager(temp_db).get(worktree.id) is None
    stored = TaskArtifactManager(temp_db).get_artifacts(task.id)
    assert stored.worktree_id is None
    assert stored.worktree_path is None


def test_successful_merge_cleanup_force_deletes_dirty_inactive_worktree(
    monkeypatch: pytest.MonkeyPatch,
    temp_db,
    sample_project,
    tmp_path: Path,
) -> None:
    from gobby.build import controls
    from gobby.storage.tasks import TaskArtifactManager
    from gobby.storage.worktrees import LocalWorktreeManager

    monkeypatch.setattr(controls, "LocalTaskManager", LocalTaskManager)
    _set_project_repo(temp_db, sample_project["id"], tmp_path)
    task_manager = LocalTaskManager(temp_db)
    task = task_manager.create_task(
        project_id=sample_project["id"],
        title="Merged dirty artifact",
        category="code",
        task_type="task",
    )
    worktree_path = tmp_path / "dirty-inactive-worktree"
    worktree_path.mkdir()
    (worktree_path / "staged-residue.txt").write_text("merge residue\n")
    worktree = LocalWorktreeManager(temp_db).create(
        project_id=sample_project["id"],
        branch_name="task-dirty-inactive-worktree",
        worktree_path=str(worktree_path),
        base_branch="0.4.7",
        task_id=task.id,
    )
    TaskArtifactManager(temp_db).set_artifacts_atomic(
        task.id,
        worktree_path=str(worktree_path),
        worktree_id=worktree.id,
        base_commit_sha="abc123",
    )

    class ForceOnlyWorktreeGitManager:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        def delete_worktree(self, path: Path, *, force: bool = False) -> SimpleNamespace:
            if not force:
                return SimpleNamespace(success=False, error="dirty", message="dirty")
            residue = Path(path) / "staged-residue.txt"
            residue.unlink()
            Path(path).rmdir()
            return SimpleNamespace(success=True, error=None, message="deleted")

    monkeypatch.setattr(controls, "WorktreeGitManager", ForceOnlyWorktreeGitManager)
    monkeypatch.setattr(controls, "delete_orphan_build_branches", lambda *_args: (0, []))

    artifacts = controls.cleanup_successful_merge_artifacts(
        temp_db,
        task.id,
        project_id=sample_project["id"],
    )

    assert len(artifacts) == 1
    assert artifacts[0].deleted is True
    assert artifacts[0].error is None
    assert not worktree_path.exists()
    assert LocalWorktreeManager(temp_db).get(worktree.id) is None


@pytest.mark.asyncio
async def test_clean_force_resets_runtime_state_without_artifacts(
    temp_db,
    sample_project,
    tmp_path: Path,
) -> None:
    from gobby.build.controls import build_clean_target
    from gobby.storage.tasks._dispatch_mutex import TaskDispatchMutexManager
    from tests.storage.tasks._stage_test_helpers import initialize_manifest, set_stage_state, spec

    _set_project_repo(temp_db, sample_project["id"], tmp_path)
    task_manager = LocalTaskManager(temp_db)
    task = task_manager.create_task(
        project_id=sample_project["id"],
        title="Clean phantom state",
        category="code",
        task_type="task",
    )
    initialize_manifest(temp_db, task.id, [spec("development", 0)])
    set_stage_state(temp_db, task.id, "development", "in_progress")

    sessions = SessionManager(temp_db)
    parent = sessions.register(
        external_id="clean-parent",
        machine_id="machine-1",
        source="codex",
        project_id=sample_project["id"],
    )
    child = sessions.register(
        external_id="clean-child",
        machine_id="machine-1",
        source="codex",
        project_id=sample_project["id"],
        parent_session_id=parent.id,
    )
    task_manager.claim_task(task.id, session_id=child.id)
    run_manager = LocalAgentRunManager(temp_db)
    run = run_manager.create(
        parent_session_id=parent.id,
        child_session_id=child.id,
        provider="codex",
        prompt="work",
        task_id=task.id,
        run_id="run-clean-phantom",
    )
    run_manager.start(run.id)
    TaskDispatchMutexManager(temp_db).acquire_mutex(
        task.id,
        holder="dispatcher",
        kind="spawn_agent",
        ttl_seconds=300,
        run_id=run.id,
    )

    with patch("gobby.build.controls.kill_agent", new=AsyncMock(return_value={"success": True})):
        result = await build_clean_target(
            f"#{task.seq_num}",
            db=temp_db,
            project_id=sample_project["id"],
            force=True,
            yes=True,
        )

    assert task_manager.get_task(task.id).claimed_by_session_id is None
    assert TaskDispatchMutexManager(temp_db).get_mutex(task.id) is None
    assert result.claims_released == 1
    assert result.mutexes_cleared >= 1
    assert result.stages_reset == 1


@pytest.mark.asyncio
async def test_restart_dry_run_reports_restart_without_mutating_task(
    temp_db,
    sample_project,
    tmp_path: Path,
) -> None:
    from gobby.build.controls import build_restart_target

    _set_project_repo(temp_db, sample_project["id"], tmp_path)
    task_manager = LocalTaskManager(temp_db)
    task = task_manager.create_task(
        project_id=sample_project["id"],
        title="Restart preview",
        category="code",
        task_type="task",
    )
    task_manager.update_task(task.id, allow_automation=True)

    result = await build_restart_target(
        f"#{task.seq_num}",
        db=temp_db,
        project_id=sample_project["id"],
        dry_run=True,
    )

    assert result.action == "restart"
    assert result.dry_run is True
    assert task_manager.get_task(task.id).allow_automation is True


@pytest.mark.asyncio
async def test_restart_reseeds_exhausted_isolated_manifest_with_merge(
    temp_db,
    sample_project,
    tmp_path: Path,
) -> None:
    from gobby.build.controls import build_restart_target
    from gobby.build.dispatch_tick import DispatcherTickSummary
    from tests.storage.tasks._stage_test_helpers import initialize_manifest, set_stage_state, spec

    _set_project_repo(temp_db, sample_project["id"], tmp_path)
    task_manager = LocalTaskManager(temp_db)
    task = task_manager.create_task(
        project_id=sample_project["id"],
        title="Exhausted docs task",
        category="docs",
        task_type="task",
    )
    task_manager.update_task(task.id, allow_automation=True, isolation="worktree")
    task_manager.artifacts.set_artifact(task.id, "target_branch", "integration/test")
    initialize_manifest(temp_db, task.id, [spec("development", 0)])
    set_stage_state(temp_db, task.id, "development", "done")

    with patch(
        "gobby.build.controls._kick_dispatcher_tick",
        new=AsyncMock(return_value=DispatcherTickSummary()),
    ):
        result = await build_restart_target(
            f"#{task.seq_num}",
            db=temp_db,
            project_id=sample_project["id"],
            force=True,
            yes=True,
        )

    rows = task_manager.stage_states.list_for_task(task.id)
    assert result.stages_reset == 1
    assert [row.stage_name for row in rows] == ["development", "merge"]
    assert {row.state for row in rows} == {"ready"}


@pytest.mark.asyncio
async def test_restart_no_resume_resets_epic_tree_without_dispatch(
    temp_db,
    sample_project,
    tmp_path: Path,
) -> None:
    from gobby.build.controls import build_restart_target
    from tests.storage.tasks._stage_test_helpers import initialize_manifest, set_stage_state, spec

    _set_project_repo(temp_db, sample_project["id"], tmp_path)
    task_manager = LocalTaskManager(temp_db)
    epic = task_manager.create_task(
        project_id=sample_project["id"],
        title="Docs epic",
        category="planning",
        task_type="epic",
    )
    leaf = task_manager.create_task(
        project_id=sample_project["id"],
        title="Docs leaf",
        category="docs",
        task_type="task",
        parent_task_id=epic.id,
    )
    task_manager.update_task(epic.id, allow_automation=True, isolation="worktree")
    task_manager.update_task(
        leaf.id,
        allow_automation=True,
        isolation="worktree",
        dispatch_failure_count=2,
    )
    initialize_manifest(temp_db, epic.id, [spec("planning", 0), spec("merge", 1)])
    initialize_manifest(temp_db, leaf.id, [spec("development", 0)])
    set_stage_state(temp_db, epic.id, "planning", "done")
    set_stage_state(temp_db, leaf.id, "development", "in_progress")
    task_manager.escalate_task(leaf.id, "development_max_work_attempts")

    with patch("gobby.build.controls._kick_dispatcher_tick", new=AsyncMock()) as tick:
        result = await build_restart_target(
            f"#{epic.seq_num}",
            db=temp_db,
            project_id=sample_project["id"],
            force=True,
            yes=True,
            no_resume=True,
        )

    epic_rows = task_manager.stage_states.list_for_task(epic.id)
    leaf_rows = task_manager.stage_states.list_for_task(leaf.id)
    assert [row.stage_name for row in epic_rows] == ["development", "holistic_qa", "merge"]
    assert [row.stage_name for row in leaf_rows] == ["development", "merge"]
    assert {row.state for row in epic_rows + leaf_rows} == {"ready"}
    assert task_manager.get_task(epic.id).allow_automation is False
    assert task_manager.get_task(leaf.id).allow_automation is False
    assert task_manager.get_task(leaf.id).dispatch_failure_count == 0
    assert task_manager.get_task(leaf.id).is_escalated is False
    assert result.dispatcher_tick is None
    tick.assert_not_called()


@pytest.mark.asyncio
async def test_restart_clears_build_owned_dispatch_escalations(
    temp_db,
    sample_project,
    tmp_path: Path,
) -> None:
    from gobby.build.controls import build_restart_target
    from gobby.build.dispatch_tick import DispatcherTickSummary

    _set_project_repo(temp_db, sample_project["id"], tmp_path)
    task_manager = LocalTaskManager(temp_db)
    epic = task_manager.create_task(
        project_id=sample_project["id"],
        title="Restart escalated epic",
        category="planning",
        task_type="epic",
    )
    auto_escalated = task_manager.create_task(
        project_id=sample_project["id"],
        title="Auto escalated child",
        category="docs",
        task_type="task",
        parent_task_id=epic.id,
    )
    manual_escalated = task_manager.create_task(
        project_id=sample_project["id"],
        title="Manual escalated child",
        category="docs",
        task_type="task",
        parent_task_id=epic.id,
    )
    task_manager.update_task(
        auto_escalated.id,
        allow_automation=True,
        dispatch_failure_count=3,
        validation_fail_count=2,
    )
    task_manager.update_task(
        manual_escalated.id,
        allow_automation=True,
        dispatch_failure_count=3,
        validation_fail_count=2,
    )
    task_manager.escalate_task(
        auto_escalated.id,
        "dispatch_spawn_max_attempts:Failed to create worktree",
    )
    task_manager.escalate_task(manual_escalated.id, "needs_human: ambiguous docs scope")

    with patch(
        "gobby.build.controls._kick_dispatcher_tick",
        new=AsyncMock(return_value=DispatcherTickSummary()),
    ):
        result = await build_restart_target(
            f"#{epic.seq_num}",
            db=temp_db,
            project_id=sample_project["id"],
            force=True,
            yes=True,
        )

    retried = task_manager.get_task(auto_escalated.id)
    preserved = task_manager.get_task(manual_escalated.id)
    assert result.escalations_cleared == 1
    assert result.dispatch_failures_reset == 2
    assert retried.is_escalated is False
    assert retried.escalated_at is None
    assert retried.escalation_reason is None
    assert retried.dispatch_failure_count == 0
    assert retried.validation_fail_count == 0
    assert preserved.is_escalated is True
    assert preserved.escalation_reason == "needs_human: ambiguous docs scope"
    assert preserved.dispatch_failure_count == 0


@pytest.mark.asyncio
async def test_restart_resets_stale_dispatch_failure_count_without_escalation(
    temp_db,
    sample_project,
    tmp_path: Path,
) -> None:
    from gobby.build.controls import build_restart_target
    from gobby.build.dispatch_tick import DispatcherTickSummary

    _set_project_repo(temp_db, sample_project["id"], tmp_path)
    task_manager = LocalTaskManager(temp_db)
    task = task_manager.create_task(
        project_id=sample_project["id"],
        title="Retry counter only",
        category="docs",
        task_type="task",
    )
    task_manager.update_task(task.id, allow_automation=True, dispatch_failure_count=3)

    with patch(
        "gobby.build.controls._kick_dispatcher_tick",
        new=AsyncMock(return_value=DispatcherTickSummary()),
    ):
        result = await build_restart_target(
            f"#{task.seq_num}",
            db=temp_db,
            project_id=sample_project["id"],
            force=True,
            yes=True,
        )

    assert result.dispatch_failures_reset == 1
    assert result.escalations_cleared == 0
    assert task_manager.get_task(task.id).dispatch_failure_count == 0


def test_default_branch_dir_name_uses_untitled_for_empty_slug(
    temp_db,
    sample_project,
) -> None:
    from gobby.build.branch_cleanup import default_task_branch_name

    task = LocalTaskManager(temp_db).create_task(
        project_id=sample_project["id"],
        title="!!!",
        category="code",
        task_type="task",
    )

    assert default_task_branch_name(task) == f"task-{task.seq_num}-untitled"
