"""Immutable native Ask pipeline contract and executor boundary."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol

from gobby.ask.permissions import ASK_PIPELINE_NAME
from gobby.workflows.pipeline_models import PipelineDefinition
from gobby.workflows.pipeline_state import PipelineExecution

ASK_PIPELINE_STEPS: dict[str, str] = {
    "prepare": "prepare",
    "seed": "seed",
    "investigate": "spawn",
    "validate_initial": "validate",
    "review_initial": "spawn",
    "admit_repair": "admit_repair",
    "repair": "spawn",
    "validate_repair": "validate",
    "review_repair": "spawn",
    "publish": "publish",
}
_REPAIR_STEPS = frozenset({"repair", "validate_repair", "review_repair"})
_REPAIR_CONDITION = "${{ steps.admit_repair.output.repair }}"


class AskPipelineExecutor(Protocol):
    """Existing pipeline executor surface required by Ask orchestration."""

    async def execute(
        self,
        pipeline: PipelineDefinition,
        inputs: dict[str, Any],
        project_id: str,
        execution_id: str | None = None,
        session_id: str | None = None,
    ) -> PipelineExecution: ...


def parse_ask_pipeline(snapshot: Mapping[str, Any]) -> PipelineDefinition:
    """Validate the executable pipeline snapshot accepted for one Ask run."""
    pipeline = PipelineDefinition.model_validate(dict(snapshot))
    if pipeline.name != ASK_PIPELINE_NAME:
        raise ValueError(f"Ask pipeline must be named {ASK_PIPELINE_NAME!r}")
    if not pipeline.enabled or not pipeline.resume_on_restart or pipeline.expose_as_tool:
        raise ValueError("Ask pipeline must be enabled, resumable, and internal")
    if tuple(step.id for step in pipeline.steps) != tuple(ASK_PIPELINE_STEPS):
        raise ValueError("Ask pipeline stage sequence is not canonical")
    for step in pipeline.steps:
        expected_tool = ASK_PIPELINE_STEPS[step.id]
        if step.mcp is None or step.mcp.server != "gobby-ask" or step.mcp.tool != expected_tool:
            raise ValueError(f"Ask pipeline step {step.id!r} is not an internal Ask operation")
        expected_condition = _REPAIR_CONDITION if step.id in _REPAIR_STEPS else None
        if step.condition != expected_condition:
            raise ValueError(f"Ask pipeline step {step.id!r} has an unsafe condition")
    return pipeline


__all__ = ["ASK_PIPELINE_STEPS", "AskPipelineExecutor", "parse_ask_pipeline"]
