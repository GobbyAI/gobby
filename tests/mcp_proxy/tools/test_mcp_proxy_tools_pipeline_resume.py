"""Tests for pipeline resume on daemon restart."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Generator
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gobby.mcp_proxy.tools.workflows._pipeline_execution import (
    _background_tasks,
    _background_tasks_by_execution,
    resume_interrupted_pipelines,
    resume_pipeline,
)
from gobby.workflows.pipeline_state import ExecutionStatus, StepStatus
from tests._timing import drain_asyncio_tasks

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _clear_background_tasks() -> Generator[None]:
    """Ensure _background_tasks is empty before and after each test."""
    _background_tasks.clear()
    _background_tasks_by_execution.clear()
    yield
    # Cancel any tasks created during the test before clearing
    for task in list(_background_tasks):
        task.cancel()
    _background_tasks.clear()
    _background_tasks_by_execution.clear()


def _make_execution(
    execution_id: str = "pe-test-1234",
    pipeline_name: str = "test-pipeline",
    status: ExecutionStatus = ExecutionStatus.RUNNING,
    inputs_json: str | None = None,
    session_id: str | None = None,
    project_id: str = "test-project",
) -> MagicMock:
    """Create a mock PipelineExecution."""
    execution = MagicMock()
    execution.id = execution_id
    execution.pipeline_name = pipeline_name
    execution.status = status
    execution.inputs_json = inputs_json
    execution.session_id = session_id
    execution.project_id = project_id
    return execution


def _make_pipeline(
    name: str = "test-pipeline",
    resume_on_restart: bool = False,
    enabled: bool = True,
) -> MagicMock:
    """Create a mock PipelineDefinition."""
    pipeline = MagicMock()
    pipeline.name = name
    pipeline.resume_on_restart = resume_on_restart
    pipeline.enabled = enabled
    return pipeline


@pytest.mark.asyncio
async def test_resume_returns_empty_when_no_running() -> None:
    """No RUNNING executions means nothing to resume."""
    loader = AsyncMock()
    executor = MagicMock()
    execution_manager = MagicMock()
    execution_manager.list_executions.return_value = []

    result = await resume_interrupted_pipelines(
        loader=loader,
        executor=executor,
        execution_manager=execution_manager,
        project_id="test-project",
    )

    assert result == []
    assert len(_background_tasks) == 0


@pytest.mark.asyncio
async def test_resume_skips_non_resumable_pipelines() -> None:
    """Pipelines without resume_on_restart=True are skipped."""
    execution = _make_execution()
    pipeline = _make_pipeline(resume_on_restart=False)

    loader = AsyncMock()
    loader.load_pipeline.return_value = pipeline
    executor = MagicMock()
    execution_manager = MagicMock()
    execution_manager.list_executions.return_value = [execution]

    result = await resume_interrupted_pipelines(
        loader=loader,
        executor=executor,
        execution_manager=execution_manager,
        project_id="test-project",
    )

    assert result == []
    assert len(_background_tasks) == 0


@pytest.mark.asyncio
async def test_resume_skips_disabled_pipelines() -> None:
    """Disabled pipelines are not restarted even when resume_on_restart is set."""
    execution = _make_execution()
    pipeline = _make_pipeline(resume_on_restart=True, enabled=False)
    loader = AsyncMock()
    loader.load_pipeline.return_value = pipeline
    executor = MagicMock()
    execution_manager = MagicMock()
    execution_manager.list_executions.return_value = [execution]

    result = await resume_interrupted_pipelines(
        loader=loader,
        executor=executor,
        execution_manager=execution_manager,
        project_id="test-project",
    )

    assert result == []
    assert len(_background_tasks) == 0


@pytest.mark.asyncio
async def test_concurrent_double_resume_spawns_one_executor() -> None:
    """Only one caller can atomically claim a failed execution for resume."""
    execution = _make_execution(
        status=ExecutionStatus.FAILED,
        inputs_json=json.dumps({"branch": "main"}),
    )
    pipeline = _make_pipeline()
    failed_step = MagicMock()
    failed_step.step_id = "failed-step"
    failed_step.status = StepStatus.FAILED
    failed_step.error = "boom"

    arrivals = 0
    both_loading = asyncio.Event()

    async def _load_pipeline(name: str, project_id: str) -> MagicMock:
        nonlocal arrivals
        assert name == execution.pipeline_name
        assert project_id == execution.project_id
        arrivals += 1
        if arrivals == 2:
            both_loading.set()
        await both_loading.wait()
        return pipeline

    loader = MagicMock()
    loader.load_pipeline = AsyncMock(side_effect=_load_pipeline)
    executor = MagicMock()
    executor.execute = AsyncMock(return_value=execution)
    execution_manager = MagicMock()
    execution_manager.get_execution.return_value = execution
    execution_manager.get_steps_for_execution.return_value = [failed_step]
    execution_manager.reset_steps_from.return_value = 1
    execution_manager.claim_failed_execution_for_resume.side_effect = [execution, None]

    results = await asyncio.gather(
        resume_pipeline(
            loader=loader,
            executor=executor,
            execution_manager=execution_manager,
            execution_id=execution.id,
            project_id=execution.project_id,
        ),
        resume_pipeline(
            loader=loader,
            executor=executor,
            execution_manager=execution_manager,
            execution_id=execution.id,
            project_id=execution.project_id,
        ),
    )
    await drain_asyncio_tasks(cycles=2)

    winners = [result for result in results if result["success"]]
    losers = [result for result in results if not result["success"]]
    assert len(winners) == 1
    assert winners[0]["status"] == "resuming"
    assert len(losers) == 1
    assert "already being resumed" in losers[0]["error"]
    assert execution_manager.claim_failed_execution_for_resume.call_count == 2
    execution_manager.reset_steps_from.assert_called_once_with(execution.id, "failed-step")
    executor.execute.assert_awaited_once()


@pytest.mark.asyncio
async def test_resume_rolls_back_claim_when_step_reset_fails() -> None:
    execution = _make_execution(status=ExecutionStatus.FAILED)
    pipeline = _make_pipeline()
    failed_step = SimpleNamespace(
        step_id="failed-step",
        status=StepStatus.FAILED,
        error="boom",
    )
    loader = SimpleNamespace(load_pipeline=AsyncMock(return_value=pipeline))
    executor = SimpleNamespace(execute=AsyncMock())
    execution_manager = MagicMock()
    execution_manager.get_execution.return_value = execution
    execution_manager.get_steps_for_execution.return_value = [failed_step]
    execution_manager.claim_failed_execution_for_resume.return_value = execution
    execution_manager.reset_steps_from.side_effect = RuntimeError("reset failed")

    with pytest.raises(RuntimeError, match="reset failed"):
        await resume_pipeline(
            loader=loader,
            executor=executor,
            execution_manager=execution_manager,
            execution_id=execution.id,
            project_id=execution.project_id,
        )

    execution_manager.update_execution_status.assert_called_once_with(
        execution.id, ExecutionStatus.FAILED
    )
    execution_manager.claim_failed_execution_for_resume.assert_called_once_with(execution.id)
    execution_manager.reset_steps_from.assert_called_once_with(execution.id, "failed-step")
    loader.load_pipeline.assert_awaited_once_with(pipeline.name, execution.project_id)
    executor.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_resume_skips_missing_pipeline_definition() -> None:
    """Executions whose pipeline definition can't be loaded are skipped."""
    execution = _make_execution()

    loader = AsyncMock()
    loader.load_pipeline.return_value = None
    executor = MagicMock()
    execution_manager = MagicMock()
    execution_manager.list_executions.return_value = [execution]

    result = await resume_interrupted_pipelines(
        loader=loader,
        executor=executor,
        execution_manager=execution_manager,
        project_id="test-project",
    )

    assert result == []


@pytest.mark.asyncio
async def test_resume_skips_when_loader_raises() -> None:
    """Executions whose pipeline loader raises are skipped gracefully."""
    execution = _make_execution()

    loader = AsyncMock()
    loader.load_pipeline.side_effect = RuntimeError("definition deleted")
    executor = MagicMock()
    execution_manager = MagicMock()
    execution_manager.list_executions.return_value = [execution]

    result = await resume_interrupted_pipelines(
        loader=loader,
        executor=executor,
        execution_manager=execution_manager,
        project_id="test-project",
    )

    assert result == []


@pytest.mark.asyncio
async def test_resume_creates_background_task_for_resumable() -> None:
    """Resumable pipelines get re-queued as background tasks."""
    execution = _make_execution(
        inputs_json=json.dumps({"branch": "main"}),
        session_id="sess-123",
    )
    pipeline = _make_pipeline(resume_on_restart=True)

    loader = AsyncMock()
    loader.load_pipeline.return_value = pipeline
    # Make executor.execute a coroutine that blocks until cancelled
    executor = MagicMock()
    executor.execute = AsyncMock(side_effect=asyncio.CancelledError)
    execution_manager = MagicMock()
    execution_manager.list_executions.return_value = [execution]

    result = await resume_interrupted_pipelines(
        loader=loader,
        executor=executor,
        execution_manager=execution_manager,
        project_id="test-project",
    )

    assert result == [execution.id]
    assert len(_background_tasks) == 1


@pytest.mark.asyncio
async def test_resume_returns_only_resumable_ids() -> None:
    """Only resumable execution IDs are returned; non-resumable are excluded."""
    resumable_exec = _make_execution(
        execution_id="pe-resumable",
        pipeline_name="resumable-pipeline",
    )
    non_resumable_exec = _make_execution(
        execution_id="pe-non-resumable",
        pipeline_name="non-resumable-pipeline",
    )
    resumable_pipeline = _make_pipeline(name="resumable-pipeline", resume_on_restart=True)
    non_resumable_pipeline = _make_pipeline(name="non-resumable-pipeline", resume_on_restart=False)

    loader = AsyncMock()

    async def _load(name: str, project_path: str | None = None) -> MagicMock:
        assert project_path == "test-project"
        if name == "resumable-pipeline":
            return resumable_pipeline
        return non_resumable_pipeline

    loader.load_pipeline.side_effect = _load
    executor = MagicMock()
    executor.execute = AsyncMock(side_effect=asyncio.CancelledError)
    execution_manager = MagicMock()
    execution_manager.list_executions.return_value = [resumable_exec, non_resumable_exec]

    result = await resume_interrupted_pipelines(
        loader=loader,
        executor=executor,
        execution_manager=execution_manager,
        project_id="test-project",
    )

    assert result == ["pe-resumable"]
    assert len(_background_tasks) == 1


@pytest.mark.asyncio
async def test_resume_parses_inputs_from_execution() -> None:
    """Stored inputs_json is parsed and passed to the background task."""
    inputs = {"repo": "gobby", "ref": "main"}
    execution = _make_execution(inputs_json=json.dumps(inputs))
    pipeline = _make_pipeline(resume_on_restart=True)

    loader = AsyncMock()
    loader.load_pipeline.return_value = pipeline
    # Block forever so the task stays in _background_tasks
    blocker = asyncio.Event()
    executor = MagicMock()
    executor.execute = AsyncMock(side_effect=lambda **kw: blocker.wait())
    execution_manager = MagicMock()
    execution_manager.list_executions.return_value = [execution]

    result = await resume_interrupted_pipelines(
        loader=loader,
        executor=executor,
        execution_manager=execution_manager,
        project_id="test-project",
    )

    assert result == [execution.id]
    assert len(_background_tasks) == 1

    await drain_asyncio_tasks()

    executor.execute.assert_called_once()
    assert executor.execute.call_args.kwargs["inputs"] == {"repo": "gobby", "ref": "main"}


@pytest.mark.asyncio
async def test_resume_handles_malformed_inputs_json() -> None:
    """Malformed inputs_json defaults to empty dict, doesn't crash."""
    execution = _make_execution(inputs_json="not valid json")
    pipeline = _make_pipeline(resume_on_restart=True)

    loader = AsyncMock()
    loader.load_pipeline.return_value = pipeline
    executor = MagicMock()
    executor.execute = AsyncMock(side_effect=asyncio.CancelledError)
    execution_manager = MagicMock()
    execution_manager.list_executions.return_value = [execution]

    # Should not raise
    result = await resume_interrupted_pipelines(
        loader=loader,
        executor=executor,
        execution_manager=execution_manager,
        project_id="test-project",
    )

    assert result == [execution.id]
    await drain_asyncio_tasks()
    assert executor.execute.call_args.kwargs["inputs"] == {}
