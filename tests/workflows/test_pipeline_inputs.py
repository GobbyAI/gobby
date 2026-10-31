"""Pipelines that declare strict inputs refuse unknown or nested keys before any execution."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from gobby.mcp_proxy.tools.workflows._pipeline_execution import run_pipeline
from gobby.workflows.pipeline_inputs import input_refusal
from gobby.workflows.pipeline_models import PipelineDefinition

pytestmark = pytest.mark.unit


def _pipeline(*, strict: bool = True) -> PipelineDefinition:
    return PipelineDefinition.model_validate(
        {
            "name": "strict",
            "strict_inputs": strict,
            "inputs": {
                "task": {"type": "string", "required": True},
                "test_writer_model": {"type": "string", "default": None},
            },
            "steps": [{"id": "guard", "mcp": {"server": "gobby-agents", "tool": "list_agents"}}],
        }
    )


def test_strict_pipeline_refuses_unknown_keys_and_names_the_flat_keys() -> None:
    refusal = input_refusal(
        _pipeline(), {"task": "#1", "launch_defaults": {"test_writer_model": "model"}}
    )

    assert refusal is not None
    assert "launch_defaults" in refusal
    assert "task" in refusal and "test_writer_model" in refusal


def test_strict_pipeline_refuses_a_nested_value_for_a_string_input() -> None:
    refusal = input_refusal(_pipeline(), {"task": "#1", "test_writer_model": {"model": "m"}})

    assert refusal is not None
    assert "test_writer_model" in refusal


def test_strict_pipeline_accepts_flat_inputs() -> None:
    assert input_refusal(_pipeline(), {"task": "#1", "test_writer_model": None}) is None


def test_lenient_pipeline_keeps_unknown_keys() -> None:
    assert input_refusal(_pipeline(strict=False), {"task": "#1", "extra": 1}) is None


@pytest.mark.asyncio
async def test_run_pipeline_refuses_nested_inputs_before_creating_an_execution() -> None:
    loader = MagicMock()
    loader.load_pipeline = AsyncMock(return_value=_pipeline())
    manager = MagicMock()
    executor = MagicMock()
    executor.execution_manager = manager

    result = await run_pipeline(
        loader, executor, "strict", {"task": "#1", "launch_defaults": {}}, "project-id"
    )

    assert result["success"] is False
    assert "launch_defaults" in result["error"]
    manager.create_execution.assert_not_called()
