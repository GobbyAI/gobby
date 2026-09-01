from __future__ import annotations

import shutil
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from gobby.runner_lifecycle_startup import StartupTracker
from gobby.runner_lifecycle_subsystems import (
    _recover_pipelines,
    _register_wiki_cron_handlers,
    _start_cron_scheduler,
)
from gobby.storage.cron import CronJobStorage
from gobby.storage.cron_models import CronJob
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.pipelines import LocalPipelineExecutionManager
from gobby.storage.projects import LocalProjectManager, Project
from gobby.storage.worktrees import LocalWorktreeManager
from gobby.workflows.pipeline_state import ExecutionStatus
from tests.fixtures.isolated_checkout import (
    insert_overlay,
    install_isolated_checkout_project,
    write_project_marker,
)

if TYPE_CHECKING:
    from gobby.runner import GobbyRunner


class RecordingCronExecutor:
    def __init__(self) -> None:
        self.handlers: dict[str, object] = {}

    def register_handler(self, name: str, handler: object) -> None:
        self.handlers[name] = handler


def _wiki_enabled_config_runtime() -> SimpleNamespace:
    active = SimpleNamespace(wiki=SimpleNamespace(enabled=True))
    bundle = SimpleNamespace(snapshot=SimpleNamespace(active=active))
    return SimpleNamespace(capture=lambda: bundle)


def _create_projects(
    temp_db: HubDatabase,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> list[Project]:
    projects = []
    machine_id: str | None = None
    for name in ("alpha", "beta"):
        isolated = install_isolated_checkout_project(
            temp_db,
            tmp_path / name,
            name=name,
            machine_id=machine_id,
            monkeypatch=monkeypatch,
        )
        machine_id = isolated.machine_id
        projects.append(isolated.project)
    return projects


def _create_wiki_job(
    cron_storage: CronJobStorage,
    *,
    project_id: str,
    enabled: bool,
) -> CronJob:
    scope = f"project:{project_id}"
    return cron_storage.create_job(
        project_id=project_id,
        name=f"gobby:wiki-refresh:{scope}",
        schedule_type="interval",
        action_type="handler",
        action_config={
            "handler": f"wiki:refresh:{scope}",
            "scope": scope,
            "command": "refresh",
        },
        interval_seconds=3600,
        enabled=enabled,
        is_system=True,
    )


@pytest.mark.asyncio
async def test_pipeline_recovery_covers_multiple_projects_outside_startup_project(
    temp_db: HubDatabase,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    projects = _create_projects(temp_db, tmp_path, monkeypatch)
    managers = [LocalPipelineExecutionManager(temp_db, project.id) for project in projects]
    stale = managers[0].create_execution(pipeline_name="stale-pipeline")
    managers[0].update_execution_status(stale.id, ExecutionStatus.RUNNING)
    interrupted = managers[1].create_execution(pipeline_name="interrupted-pipeline")
    managers[1].update_execution_status(interrupted.id, ExecutionStatus.INTERRUPTED)

    subscriber_ids = [str(uuid4()), str(uuid4())]
    managers[0].add_completion_subscribers(stale.id, [subscriber_ids[0]])
    managers[1].add_completion_subscribers(interrupted.id, [subscriber_ids[1]])

    loader = AsyncMock()
    loader.load_pipeline.return_value = MagicMock(resume_on_restart=False)
    completion_registry = MagicMock()
    completion_registry.notify = AsyncMock()
    db_run = AsyncMock(side_effect=lambda operation, *args, **kwargs: operation(*args, **kwargs))
    runner = SimpleNamespace(
        database=temp_db,
        workflow_loader=loader,
        project_id=None,
        pipeline_execution_manager=None,
        pipeline_executor=None,
        completion_registry=completion_registry,
        _shutdown_requested=False,
        llm_service=MagicMock(),
        session_manager=MagicMock(),
        db_executor=SimpleNamespace(run=db_run),
    )
    tracker = StartupTracker()
    monkeypatch.setattr(
        "gobby.runner_lifecycle_subsystems._PROJECT_ENUMERATION_PAGE_SIZE",
        1,
    )

    await _recover_pipelines(cast("GobbyRunner", runner), tracker)

    stored_stale = managers[0].get_execution(stale.id)
    stored_interrupted = managers[1].get_execution(interrupted.id)
    assert stored_stale is not None
    assert stored_interrupted is not None
    assert stored_stale.status is ExecutionStatus.INTERRUPTED
    assert stored_interrupted.status is ExecutionStatus.INTERRUPTED
    assert completion_registry.notify.await_count == 2
    assert managers[0].get_completion_subscribers(stale.id) == []
    assert managers[1].get_completion_subscribers(interrupted.id) == []
    assert "Pipeline recovery" in tracker.steps_completed
    assert tracker.errors == []

    loader.load_pipeline.assert_awaited_once_with(
        "stale-pipeline",
        project_path=projects[0].id,
    )
    offloaded_operations = {call.args[0].__name__ for call in db_run.await_args_list}
    assert {
        "list_recovery_project_ids",
        "list_executions",
        "interrupt_stale_running_executions",
        "get_completion_subscribers",
        "remove_completion_subscribers",
    } <= offloaded_operations


@pytest.mark.asyncio
async def test_wiki_cron_registers_each_project_outside_startup_project(
    temp_db: HubDatabase,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    projects = _create_projects(temp_db, tmp_path, monkeypatch)
    cron_storage = CronJobStorage(temp_db)
    executor = RecordingCronExecutor()
    db_run = AsyncMock(side_effect=lambda operation, *args, **kwargs: operation(*args, **kwargs))
    runner = SimpleNamespace(
        database=temp_db,
        project_id=None,
        config=SimpleNamespace(wiki=SimpleNamespace(scheduled_scopes=[])),
        config_runtime=_wiki_enabled_config_runtime(),
        cron_storage=cron_storage,
        cron_scheduler=SimpleNamespace(executor=executor),
        db_executor=SimpleNamespace(run=db_run),
    )
    tracker = StartupTracker()
    monkeypatch.setattr(
        "gobby.runner_lifecycle_subsystems._PROJECT_ENUMERATION_PAGE_SIZE",
        1,
    )

    await _register_wiki_cron_handlers(cast("GobbyRunner", runner), tracker)

    assert len(executor.handlers) == 17
    assert all(len(cron_storage.list_jobs(project_id=project.id)) == 8 for project in projects)
    assert "Wiki cron handlers" in tracker.steps_completed
    assert tracker.errors == []
    offloaded_operations = {call.args[0].__name__ for call in db_run.await_args_list}
    assert offloaded_operations == {
        "_discover_wiki_cron_project_scopes",
        "_ensure_wiki_cron_job",
        "_reconcile_installed_wiki_recap_timeouts",
        "list_system_jobs_by_name_prefix",
        "register_wiki_prune_cron",
    }


@pytest.mark.asyncio
async def test_wiki_cron_purges_stale_projects_and_restores_fresh_jobs(
    temp_db: HubDatabase,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_manager = LocalProjectManager(temp_db)
    live_root = tmp_path / "live"
    live_checkout = install_isolated_checkout_project(
        temp_db,
        live_root,
        name="live",
        monkeypatch=monkeypatch,
    )
    live = live_checkout.project
    monkeypatch.setattr(
        "gobby.storage.worktrees.require_machine_id",
        lambda: live_checkout.machine_id,
    )
    empty = project_manager.create(name="empty")
    missing_root = tmp_path / "missing"
    missing_checkout = install_isolated_checkout_project(
        temp_db,
        missing_root,
        name="missing",
        machine_id=live_checkout.machine_id,
    )
    missing = missing_checkout.project
    shutil.rmtree(missing_root)
    overlay_root = tmp_path / "overlay-only"
    overlay_root.mkdir()
    overlay = project_manager.create(name="overlay-only")
    insert_overlay(
        temp_db,
        project_id=overlay.id,
        machine_id=live_checkout.machine_id,
        path=str(overlay_root),
        kind="worktree",
    )
    foreign_checkout = install_isolated_checkout_project(
        temp_db,
        tmp_path / "foreign-only",
        name="foreign-only",
    )
    foreign = foreign_checkout.project
    cron_storage = CronJobStorage(temp_db)
    live_job = _create_wiki_job(cron_storage, project_id=live.id, enabled=False)
    empty_job = _create_wiki_job(cron_storage, project_id=empty.id, enabled=False)
    missing_job = _create_wiki_job(cron_storage, project_id=missing.id, enabled=False)
    overlay_job = _create_wiki_job(cron_storage, project_id=overlay.id, enabled=False)
    foreign_job = _create_wiki_job(cron_storage, project_id=foreign.id, enabled=False)
    for stale_job in (empty_job, missing_job, overlay_job, foreign_job):
        cron_storage.create_run(stale_job.id)
    operator_owned = cron_storage.create_job(
        project_id=missing.id,
        name=f"operator:wiki:project:{missing.id}",
        schedule_type="interval",
        action_type="handler",
        action_config={"handler": "operator.wiki"},
        interval_seconds=3600,
        is_system=False,
    )
    unrelated_system = cron_storage.create_job(
        project_id=missing.id,
        name=f"gobby:unrelated:project:{missing.id}",
        schedule_type="interval",
        action_type="handler",
        action_config={"handler": "unrelated.system"},
        interval_seconds=3600,
        is_system=True,
    )
    executor = RecordingCronExecutor()
    db_run = AsyncMock(side_effect=lambda operation, *args, **kwargs: operation(*args, **kwargs))
    runner = SimpleNamespace(
        database=temp_db,
        config_runtime=_wiki_enabled_config_runtime(),
        cron_storage=cron_storage,
        cron_scheduler=SimpleNamespace(executor=executor),
        db_executor=SimpleNamespace(run=db_run),
    )
    tracker = StartupTracker()

    with caplog.at_level("INFO", logger="gobby.runner_lifecycle"):
        await _register_wiki_cron_handlers(cast("GobbyRunner", runner), tracker)

    stored_live_job = cron_storage.get_job(live_job.id)
    assert stored_live_job is not None
    assert stored_live_job.enabled is False
    assert cron_storage.get_job(empty_job.id) is None
    assert cron_storage.get_job(missing_job.id) is None
    assert cron_storage.get_job(overlay_job.id) is None
    assert cron_storage.get_job(foreign_job.id) is None
    for stale_job in (empty_job, missing_job, overlay_job, foreign_job):
        assert cron_storage.list_runs(stale_job.id) == []
    assert LocalWorktreeManager(temp_db).get_by_path(str(overlay_root)) is not None
    assert cron_storage.get_job(operator_owned.id) is not None
    assert cron_storage.get_job(unrelated_system.id) is not None
    assert len(cron_storage.list_jobs(project_id=live.id)) == 8
    assert tracker.errors == []
    assert "Wiki cron handlers" in tracker.steps_completed
    assert str(empty.id) in caplog.text
    assert str(missing.id) in caplog.text
    assert caplog.text.count("Deleted 1 stale wiki cron job(s)") == 4

    await _register_wiki_cron_handlers(cast("GobbyRunner", runner), StartupTracker())
    assert len(cron_storage.list_jobs(project_id=live.id)) == 8

    missing_root.mkdir()
    write_project_marker(missing_root, project_id=missing.id, name=missing.name)
    restored_tracker = StartupTracker()
    await _register_wiki_cron_handlers(cast("GobbyRunner", runner), restored_tracker)

    restored_wiki_jobs = [
        job
        for job in cron_storage.list_jobs(project_id=missing.id)
        if job.is_system and job.name.startswith("gobby:wiki-")
    ]
    assert len(restored_wiki_jobs) == 8
    assert all(job.enabled for job in restored_wiki_jobs)
    assert restored_tracker.errors == []


@pytest.mark.asyncio
async def test_wiki_cron_stale_only_projects_complete_after_cleanup(
    temp_db: HubDatabase,
) -> None:
    stale = LocalProjectManager(temp_db).create(name="stale", repo_path=None)
    cron_storage = CronJobStorage(temp_db)
    stale_job = _create_wiki_job(cron_storage, project_id=stale.id, enabled=True)
    executor = RecordingCronExecutor()
    db_run = AsyncMock(side_effect=lambda operation, *args, **kwargs: operation(*args, **kwargs))
    runner = SimpleNamespace(
        database=temp_db,
        config_runtime=_wiki_enabled_config_runtime(),
        cron_storage=cron_storage,
        cron_scheduler=SimpleNamespace(executor=executor),
        db_executor=SimpleNamespace(run=db_run),
    )
    tracker = StartupTracker()

    await _register_wiki_cron_handlers(cast("GobbyRunner", runner), tracker)

    assert cron_storage.get_job(stale_job.id) is None
    assert tracker.errors == []
    assert "Wiki cron handlers" in tracker.steps_completed
    offloaded_operations = [call.args[0].__name__ for call in db_run.await_args_list]
    assert "delete_system_jobs_by_project_and_name_prefix" in offloaded_operations


@pytest.mark.asyncio
async def test_missing_pipeline_loader_and_cron_storage_are_tracked() -> None:
    pipeline_tracker = StartupTracker()
    await _recover_pipelines(
        cast("GobbyRunner", SimpleNamespace(workflow_loader=None)),
        pipeline_tracker,
    )
    assert pipeline_tracker.errors == [
        {"subsystem": "Pipeline recovery", "error": "skipped: workflow loader unavailable"}
    ]

    wiki_tracker = StartupTracker()
    await _start_cron_scheduler(
        cast("GobbyRunner", SimpleNamespace(cron_storage=None, cron_scheduler=None)),
        wiki_tracker,
    )
    assert wiki_tracker.errors == [
        {"subsystem": "Wiki cron handlers", "error": "skipped: cron storage unavailable"}
    ]
