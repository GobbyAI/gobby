"""Admission call sites: spawn_agent and pipeline launches refuse while the fleet is unhealthy."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from gobby.mcp_proxy.tools.spawn_agent._implementation import spawn_agent_impl
from gobby.workflows.pipeline_executor import PipelineExecutor
from gobby.workflows.pipeline_state import ExecutionStatus, PipelineStepRefusal
from tests.fleet_health.support import write_fleet_state

pytestmark = pytest.mark.unit


@pytest.mark.asyncio
async def test_spawn_agent_refuses_with_a_typed_error_while_degraded() -> None:
    write_fleet_state(state="degraded", reasons=("load_per_core:2.6",))

    result = await spawn_agent_impl(prompt="check the fleet", runner=MagicMock())

    assert result["success"] is False
    assert result["error_code"] == "fleet_unhealthy"
    assert result["error"].startswith("fleet_unhealthy")
    assert result["fleet"] == {"state": "degraded", "reasons": ["load_per_core:2.6"]}


@pytest.mark.asyncio
async def test_a_healthy_fleet_passes_spawns_through_to_their_own_checks() -> None:
    write_fleet_state(state="healthy")

    # No parent session: the spawn's own validation refuses it, never the fleet gate.
    for _ in range(5):
        result = await spawn_agent_impl(prompt="check the fleet", runner=MagicMock())
        assert result["success"] is False
        assert "error_code" not in result


@pytest.mark.asyncio
async def test_detached_pipeline_start_refuses_before_any_run_is_recorded() -> None:
    write_fleet_state(state="unstable", reasons=("probe_failed:refused",))
    execution_manager = MagicMock()
    executor = PipelineExecutor(
        db=MagicMock(),
        execution_manager=execution_manager,
        llm_service=AsyncMock(),
        tool_proxy_getter=lambda: AsyncMock(),
    )
    pipeline = MagicMock()
    pipeline.name = "fixture-pipeline"
    pipeline.enabled = True

    with pytest.raises(PipelineStepRefusal, match="fleet_unhealthy") as refusal:
        await executor.start_detached(pipeline=pipeline, inputs={}, project_id="proj-1")

    message = str(refusal.value)
    assert "daemon health is unstable" in message
    assert "probe_failed:refused" in message
    # The typed fields travel with the exception, not only in its message.
    assert refusal.value.refusal is not None
    assert refusal.value.refusal.code == "fleet_unhealthy"
    assert refusal.value.refusal.state == "unstable"
    assert refusal.value.refusal.reasons == ("probe_failed:refused",)
    execution_manager.create_execution.assert_not_called()
    execution_manager.update_execution_status.assert_not_called()


@pytest.mark.asyncio
async def test_resuming_a_refused_record_is_gated_by_admission() -> None:
    write_fleet_state(state="unstable", reasons=("probe_failed:refused",))
    execution_manager = MagicMock()
    execution_manager.get_execution.return_value = MagicMock(
        id="exec-1", status=ExecutionStatus.FAILED
    )
    # A refused run is persisted FAILED with no step rows: it never started.
    execution_manager.get_steps_for_execution.return_value = []
    executor = PipelineExecutor(
        db=MagicMock(),
        execution_manager=execution_manager,
        llm_service=AsyncMock(),
        tool_proxy_getter=lambda: AsyncMock(),
    )
    pipeline = MagicMock()
    pipeline.name = "fixture-pipeline"
    pipeline.enabled = True

    with pytest.raises(PipelineStepRefusal, match="fleet_unhealthy"):
        await executor._execute(
            pipeline=pipeline, inputs={}, project_id="proj-1", execution_id="exec-1"
        )

    # The gate reads the step rows to decide the record never started, and the resume
    # claim is never taken. The refusal is recorded as FAILED with its reason.
    execution_manager.get_steps_for_execution.assert_called_once_with("exec-1")
    execution_manager.claim_failed_execution_for_resume.assert_not_called()
    update = execution_manager.update_execution_status.call_args
    assert update is not None
    assert update.kwargs["status"] == ExecutionStatus.FAILED
    assert "fleet_unhealthy" in update.kwargs["outputs_json"]


@pytest.mark.asyncio
async def test_pipeline_launch_refuses_while_the_fleet_is_unstable() -> None:
    write_fleet_state(state="unstable", reasons=("probe_failed:refused",))
    execution_manager = MagicMock()
    tool_proxy = AsyncMock()
    executor = PipelineExecutor(
        db=MagicMock(),
        execution_manager=execution_manager,
        llm_service=AsyncMock(),
        tool_proxy_getter=lambda: tool_proxy,
    )
    pipeline = MagicMock()
    pipeline.name = "fixture-pipeline"
    pipeline.steps = []

    # A refused fresh run leaves no execution record, the same as a detached start.
    with pytest.raises(PipelineStepRefusal, match="fleet_unhealthy") as refusal:
        await executor.execute(pipeline=pipeline, inputs={}, project_id="proj-1")

    assert refusal.value.refusal is not None
    assert refusal.value.refusal.code == "fleet_unhealthy"
    tool_proxy.call_tool.assert_not_called()
    execution_manager.create_execution.assert_not_called()
    execution_manager.update_execution_status.assert_not_called()
