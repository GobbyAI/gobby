from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from gobby.runner_lifecycle_startup import StartupTracker
from gobby.runner_lifecycle_subsystems import (
    _recover_pipelines,
)
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.pipelines import LocalPipelineExecutionManager
from gobby.storage.projects import Project
from gobby.workflows.pipeline_state import ExecutionStatus
from tests.fixtures.isolated_checkout import (
    install_isolated_checkout_project,
)

if TYPE_CHECKING:
    from gobby.runner import GobbyRunner


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
