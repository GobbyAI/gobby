from __future__ import annotations

import asyncio
import os
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from gobby.config.bootstrap import BootstrapConfig
from gobby.runner import GobbyRunner
from gobby.runner_init.storage import open_storage_and_config
from gobby.runner_lifecycle_startup import StartupTracker
from gobby.runner_lifecycle_subsystems import (
    _recover_pipelines,
)
from gobby.storage.hub.protocol import HubDatabase, Row
from gobby.storage.pipelines import LocalPipelineExecutionManager
from gobby.storage.projects import Project
from gobby.utils.datetime import utc_now
from gobby.workflows.definitions import PipelineDefinition, PipelineStep
from gobby.workflows.pipeline_state import ExecutionStatus, StepStatus
from tests.fixtures.isolated_checkout import (
    install_isolated_checkout_project,
)


@pytest.mark.asyncio
@pytest.mark.parametrize("daemon_skew", [-86400, 86400], ids=["db-ahead", "db-behind"])
@pytest.mark.parametrize("admitted_seconds", [0, 1], ids=["at-start", "after-start"])
@pytest.mark.parametrize(
    ("status", "resumable"),
    [
        (ExecutionStatus.RUNNING, True),
        (ExecutionStatus.RUNNING, False),
        (ExecutionStatus.PENDING, False),
        (ExecutionStatus.INTERRUPTED, False),
    ],
    ids=["replay", "interrupt", "pending-cleanup", "notifications"],
)
async def test_recovery_preserves_runs_admitted_after_start(
    temp_db: HubDatabase,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    status: ExecutionStatus,
    resumable: bool,
    admitted_seconds: int,
    daemon_skew: int,
) -> None:
    project = _create_projects(temp_db, tmp_path, monkeypatch)[0]
    manager = LocalPipelineExecutionManager(temp_db, project.id)
    # Simulate a recovery backlog longer than the stale-PENDING threshold.
    db_started_at = utc_now() - timedelta(minutes=10)
    started_at = _initialize_recovery_clock(temp_db, monkeypatch, db_started_at, daemon_skew)
    definition = PipelineDefinition(
        name="admitted",
        resume_on_restart=resumable,
        steps=[PipelineStep(id="work", exec="echo recovered")],
    )
    admitted = manager.create_execution(
        pipeline_name=definition.name, definition_json=definition.model_dump_json()
    )
    manager.update_execution_status(admitted.id, status)
    admitted_at = db_started_at + timedelta(seconds=admitted_seconds)
    temp_db.execute(
        "UPDATE pipeline_executions SET created_at = %s, updated_at = %s WHERE id = %s",
        (admitted_at, admitted_at, admitted.id),
    )
    step = manager.create_step_execution(execution_id=admitted.id, step_id="work")
    manager.update_step_execution(step_execution_id=step.id, status=StepStatus.RUNNING)
    orphan = manager.create_execution(
        pipeline_name=definition.name, definition_json=definition.model_dump_json()
    )
    manager.update_execution_status(orphan.id, status)
    orphan_at = db_started_at - timedelta(seconds=1)
    temp_db.execute(
        "UPDATE pipeline_executions SET created_at = %s, updated_at = %s WHERE id = %s",
        (orphan_at, orphan_at, orphan.id),
    )
    subscriber_id = str(uuid4())
    manager.add_completion_subscribers(admitted.id, [subscriber_id])
    manager.add_completion_subscribers(orphan.id, [subscriber_id])
    completion_registry = MagicMock()
    completion_registry.notify = AsyncMock()
    loader = AsyncMock()
    loader.load_pipeline.return_value = definition
    replay = AsyncMock()
    replay_tasks: list[asyncio.Task[None]] = []

    def register_replay(execution_id: str, task: asyncio.Task[None]) -> None:
        replay_tasks.append(task)

    monkeypatch.setattr(
        "gobby.mcp_proxy.tools.workflows._pipeline_execution._register_background_task",
        register_replay,
    )
    monkeypatch.setattr(
        "gobby.mcp_proxy.tools.workflows._pipeline_execution._execute_pipeline_background",
        replay,
    )
    runner = SimpleNamespace(
        database=temp_db,
        workflow_loader=loader,
        project_id=project.id,
        pipeline_execution_manager=manager,
        pipeline_executor=MagicMock(),
        completion_registry=completion_registry,
        _shutdown_requested=False,
        started_at=started_at,
        services=SimpleNamespace(restart_recovery_ready=False),
        db_executor=SimpleNamespace(
            run=AsyncMock(side_effect=lambda operation, *args, **kwargs: operation(*args, **kwargs))
        ),
    )

    await _recover_pipelines(cast("GobbyRunner", runner), None)
    await asyncio.gather(*replay_tasks)

    stored = manager.get_execution(admitted.id)
    assert stored is not None
    assert stored.status is status
    assert stored.updated_at == admitted_at
    assert manager.get_steps_for_execution(admitted.id)[0].status is StepStatus.RUNNING
    assert manager.get_completion_subscribers(admitted.id) == [subscriber_id]
    stored_orphan = manager.get_execution(orphan.id)
    assert stored_orphan is not None
    if resumable:
        assert stored_orphan.status is ExecutionStatus.RUNNING
        replay.assert_awaited_once()
        assert replay.await_args is not None
        assert replay.await_args.args[4] == orphan.id
    else:
        assert stored_orphan.status is ExecutionStatus.INTERRUPTED
        replay.assert_not_called()
        completion_registry.notify.assert_awaited_once()
        assert completion_registry.notify.await_args is not None
        assert completion_registry.notify.await_args.args[0] == orphan.id
    if status is ExecutionStatus.RUNNING:
        loader.load_pipeline.assert_awaited_once_with(definition.name, project_path=project.id)
    else:
        loader.load_pipeline.assert_not_awaited()
    assert runner.services.restart_recovery_ready is False


def _initialize_recovery_clock(
    database: HubDatabase,
    monkeypatch: pytest.MonkeyPatch,
    database_time: datetime | None,
    daemon_skew: int,
) -> datetime:
    """Exercise shared startup storage with independent daemon and database clocks."""
    import gobby.runner_init.storage as storage

    daemon_time = (database_time or utc_now()) + timedelta(seconds=daemon_skew)
    monkeypatch.setattr("gobby.runner.utc_now", lambda: daemon_time, raising=False)
    monkeypatch.setattr("gobby.utils.datetime.utc_now", lambda: daemon_time)
    fetchone = database.fetchone

    def fetch_with_clock(sql: str, params: Sequence[Any] | Mapping[str, Any] = ()) -> Row | None:
        if "clock_timestamp()" in sql:
            if database_time is None:
                return None
            return {"started_at": database_time}
        return fetchone(sql, params)

    monkeypatch.setattr(database, "fetchone", fetch_with_clock)
    monkeypatch.setattr(
        storage,
        "load_bootstrap",
        lambda *args, **kwargs: BootstrapConfig(database_url=os.environ["DATABASE_URL"]),
    )
    monkeypatch.setattr(storage, "init_hub_database", lambda _config: database)
    monkeypatch.setattr(storage, "setup_file_logging", lambda *args, **kwargs: None)
    monkeypatch.setattr(storage, "_ensure_headless_settings", lambda: None)
    monkeypatch.setattr(storage, "ensure_local_api_key", lambda *args: None)
    monkeypatch.setattr(
        "gobby.storage.model_metadata.ModelMetadataStore.populate", lambda self: None
    )
    runner = GobbyRunner.__new__(GobbyRunner)
    runner._prepare_base_state()
    open_storage_and_config(runner, None, False)
    return runner.started_at


def test_startup_refuses_missing_database_clock(
    temp_db: HubDatabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(RuntimeError, match="database startup clock is unavailable"):
        _initialize_recovery_clock(temp_db, monkeypatch, None, 0)


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
        started_at=utc_now(),
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
async def test_missing_pipeline_loader_is_tracked() -> None:
    pipeline_tracker = StartupTracker()
    await _recover_pipelines(
        cast("GobbyRunner", SimpleNamespace(workflow_loader=None)),
        pipeline_tracker,
    )
    assert pipeline_tracker.errors == [
        {"subsystem": "Pipeline recovery", "error": "skipped: workflow loader unavailable"}
    ]
