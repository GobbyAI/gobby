"""Tests for detached pipeline runs and the executor startup sweep (#17756).

Split-off executor behaviors live in test_pipeline_executor_core.py and
siblings; this module covers PipelineExecutor.start_detached and
PipelineExecutor.startup_sweep.
"""

import asyncio
import logging
from unittest.mock import AsyncMock, MagicMock

import pytest

from gobby.workflows.pipeline_state import ApprovalRequired, ExecutionStatus
from tests._timing import drain_asyncio_tasks

pytestmark = pytest.mark.unit


def _make_executor(mock_db, mock_execution_manager, mock_llm_service):
    from gobby.workflows.pipeline_executor import PipelineExecutor

    return PipelineExecutor(
        db=mock_db,
        execution_manager=mock_execution_manager,
        llm_service=mock_llm_service,
    )


class TestStartDetached:
    """Tests for PipelineExecutor.start_detached()."""

    @pytest.mark.asyncio
    async def test_start_detached_completes(
        self, mock_db, mock_execution_manager, mock_llm_service, simple_pipeline
    ) -> None:
        """start_detached returns a RUNNING execution immediately and the run
        completes in the background (acceptance 1.6.1)."""
        running = MagicMock()
        running.id = "pe-test-123"
        running.status = ExecutionStatus.RUNNING
        mock_execution_manager.update_execution_status.return_value = running

        executor = _make_executor(mock_db, mock_execution_manager, mock_llm_service)

        execution = await executor.start_detached(
            pipeline=simple_pipeline,
            inputs={},
            project_id="proj-123",
        )

        assert mock_execution_manager.create_execution.call_args.kwargs["project_id"] == "proj-123"

        # Returned immediately with a RUNNING record; the record was created
        # by start_detached, not by the background execute() resume path.
        assert execution.status == ExecutionStatus.RUNNING
        mock_execution_manager.create_execution.assert_called_once()

        assert executor._detached_tasks, "background task must be retained"

        task = next(iter(executor._detached_tasks))
        await task

        # Retention set is drained by the done-callback and the background
        # run drove the execution to COMPLETED.
        assert executor._detached_tasks == set()
        assert executor._detached_execution_ids == set()
        statuses = [
            call.kwargs.get("status")
            for call in mock_execution_manager.update_execution_status.call_args_list
        ]
        assert ExecutionStatus.COMPLETED in statuses
        # The background execute() resumed the pre-created record instead of
        # creating a second one.
        mock_execution_manager.create_execution.assert_called_once()

    @pytest.mark.asyncio
    async def test_start_detached_rejects_disabled_pipeline(
        self, mock_db, mock_execution_manager, mock_llm_service, simple_pipeline
    ) -> None:
        simple_pipeline.enabled = False
        executor = _make_executor(mock_db, mock_execution_manager, mock_llm_service)

        with pytest.raises(ValueError, match="Pipeline 'test-pipeline' is disabled"):
            await executor.start_detached(
                pipeline=simple_pipeline,
                inputs={},
                project_id="proj-123",
            )

        mock_execution_manager.create_execution.assert_not_called()

    @pytest.mark.asyncio
    async def test_start_detached_failure_is_logged_and_discarded(
        self,
        mock_db,
        mock_execution_manager,
        mock_llm_service,
        simple_pipeline,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A detached run that raises is logged as an error and discarded."""
        executor = _make_executor(mock_db, mock_execution_manager, mock_llm_service)
        executor.execute = AsyncMock(side_effect=RuntimeError("boom"))

        with caplog.at_level(logging.ERROR, logger="gobby.workflows.pipeline_executor"):
            await executor.start_detached(
                pipeline=simple_pipeline,
                inputs={},
                project_id="proj-123",
            )
            task = next(iter(executor._detached_tasks))
            with pytest.raises(RuntimeError, match="boom"):
                await task
            # Done-callbacks run via call_soon; drain scheduled callbacks.
            await drain_asyncio_tasks()

        assert executor._detached_tasks == set()
        assert executor._detached_execution_ids == set()
        assert any("Detached pipeline run" in record.message for record in caplog.records)

    @pytest.mark.asyncio
    async def test_start_detached_approval_park_is_not_an_error(
        self,
        mock_db,
        mock_execution_manager,
        mock_llm_service,
        simple_pipeline,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """ApprovalRequired from a detached run parks the execution; it must
        not be reported as a failed background task."""
        executor = _make_executor(mock_db, mock_execution_manager, mock_llm_service)
        executor.execute = AsyncMock(
            side_effect=ApprovalRequired(
                execution_id="pe-test-123",
                step_id="gate",
                token="tok-1",
                message="approve me",
            )
        )

        with caplog.at_level(logging.INFO, logger="gobby.workflows.pipeline_executor"):
            await executor.start_detached(
                pipeline=simple_pipeline,
                inputs={},
                project_id="proj-123",
            )
            task = next(iter(executor._detached_tasks))
            with pytest.raises(ApprovalRequired):
                await task
            await drain_asyncio_tasks()

        assert executor._detached_tasks == set()
        error_records = [r for r in caplog.records if r.levelno >= logging.ERROR]
        assert error_records == []


class TestStartupSweep:
    """Tests for PipelineExecutor.startup_sweep()."""

    def test_startup_sweep_marks_orphans_failed(
        self, mock_db, mock_execution_manager, mock_llm_service
    ) -> None:
        """Executor startup marks restart-orphaned RUNNING executions FAILED
        (acceptance 1.6.3). A fresh executor has no detached tasks, so every
        RUNNING execution in scope is an orphan."""
        mock_execution_manager.fail_stale_running_executions.return_value = 2

        executor = _make_executor(mock_db, mock_execution_manager, mock_llm_service)
        count = executor.startup_sweep()

        assert count == 2
        mock_execution_manager.fail_stale_running_executions.assert_called_once_with(
            exclude_ids=set()
        )

    @pytest.mark.asyncio
    async def test_startup_sweep_excludes_inflight_detached_runs(
        self, mock_db, mock_execution_manager, mock_llm_service, simple_pipeline
    ) -> None:
        """A sweep while a detached run is in flight must not fail it."""
        mock_execution_manager.fail_stale_running_executions.return_value = 0
        executor = _make_executor(mock_db, mock_execution_manager, mock_llm_service)

        release = asyncio.Event()

        async def _blocked_execute(*args, **kwargs):
            await release.wait()

        executor.execute = AsyncMock(side_effect=_blocked_execute)

        execution = await executor.start_detached(
            pipeline=simple_pipeline,
            inputs={},
            project_id="proj-123",
        )

        assert execution.id in executor._detached_execution_ids
        executor.startup_sweep()
        mock_execution_manager.fail_stale_running_executions.assert_called_once_with(
            exclude_ids={execution.id}
        )

        release.set()
        await next(iter(executor._detached_tasks))
        assert executor._detached_execution_ids == set()
