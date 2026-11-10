"""The MCP pipeline launch refuses with the typed fleet result before any record exists."""

import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from gobby.mcp_proxy.tools.workflows._pipeline_execution import run_pipeline
from gobby.workflows.definitions import PipelineDefinition, PipelineStep
from tests.fleet_health.support import write_fleet_state

pytestmark = pytest.mark.unit


def _definition() -> PipelineDefinition:
    return PipelineDefinition(
        name="fixture-pipeline",
        steps=[PipelineStep(id="step1", exec="echo step1")],
    )


def _loader() -> MagicMock:
    loader = MagicMock()
    loader.load_pipeline = AsyncMock(return_value=_definition())
    return loader


def _executor() -> MagicMock:
    executor = MagicMock()
    executor.execution_manager.create_execution.return_value = MagicMock(id="pe-fixture-1")
    executor.execute = AsyncMock()
    return executor


@pytest.mark.parametrize(
    ("state", "reasons", "age_seconds", "expected_state"),
    [
        ("degraded", ("load_per_core:2.9",), 0, "degraded"),
        ("unstable", ("probe_failed:refused",), 0, "unstable"),
        # A sample older than 90 seconds counts as unstable, whatever it last said.
        ("healthy", (), 120, "unstable"),
    ],
)
@pytest.mark.asyncio
async def test_a_launch_refuses_while_the_fleet_is_not_healthy(
    state: str, reasons: tuple[str, ...], age_seconds: int, expected_state: str
) -> None:
    write_fleet_state(at=time.time() - age_seconds, state=state, reasons=reasons)
    executor = _executor()

    started = await run_pipeline(
        loader=_loader(),
        executor=executor,
        name="fixture-pipeline",
        inputs={},
        project_id="proj-1",
    )

    assert started["success"] is False
    assert started["error_code"] == "fleet_unhealthy"
    assert started["fleet"]["state"] == expected_state
    assert set(reasons) <= set(started["fleet"]["reasons"])
    executor.execution_manager.create_execution.assert_not_called()
    executor.execute.assert_not_called()


@pytest.mark.asyncio
async def test_a_launch_is_admitted_while_the_fleet_is_healthy() -> None:
    write_fleet_state(state="healthy")
    executor = _executor()

    started = await run_pipeline(
        loader=_loader(),
        executor=executor,
        name="fixture-pipeline",
        inputs={},
        project_id="proj-1",
    )

    assert started["success"] is True
    assert started["status"] == "running"
    executor.execution_manager.create_execution.assert_called_once()
