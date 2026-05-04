"""Tests for task-scoped build lifecycle controls."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from gobby.storage.agents import LocalAgentRunManager
from gobby.storage.clones import LocalCloneManager
from gobby.storage.projects import LocalProjectManager
from gobby.storage.sessions import SessionManager
from gobby.storage.tasks import LocalTaskManager

pytestmark = pytest.mark.unit


def _set_project_repo(temp_db, project_id: str, tmp_path: Path) -> Path:
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    LocalProjectManager(temp_db).update(project_id, repo_path=str(repo_path))
    return repo_path


def _tree(task_manager: LocalTaskManager, project_id: str) -> tuple[object, list[object]]:
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
