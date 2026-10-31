"""Input-shape checks for pipelines that declare strict inputs."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from gobby.workflows.pipeline_models import PipelineDefinition


def input_refusal(pipeline: PipelineDefinition, inputs: Mapping[str, Any]) -> str | None:
    """Why ``inputs`` cannot launch ``pipeline``, or None when they can.

    A strict pipeline takes only its declared flat inputs: an undeclared key is refused, and a
    string input refuses a nested object or list. A lenient pipeline accepts any inputs.
    """
    if not pipeline.strict_inputs:
        return None
    unknown = sorted(key for key in inputs if key not in pipeline.inputs)
    if unknown:
        return (
            f"pipeline '{pipeline.name}' takes flat inputs "
            f"{', '.join(sorted(pipeline.inputs))}; refused: {', '.join(unknown)}"
        )
    for key, value in inputs.items():
        declared = pipeline.inputs[key]
        is_string = isinstance(declared, dict) and declared.get("type") == "string"
        if is_string and isinstance(value, dict | list):
            return f"pipeline '{pipeline.name}' input '{key}' takes a string, not a nested value"
    return None
