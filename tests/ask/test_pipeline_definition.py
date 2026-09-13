from __future__ import annotations

from pathlib import Path

import yaml

from gobby.ask.pipeline import ASK_PIPELINE_STEPS, parse_ask_pipeline
from gobby.mcp_proxy.tools.ask import create_ask_registry
from gobby.workflows.pipeline_models import PipelineDefinition


def _bundled_pipeline() -> PipelineDefinition:
    path = Path(__file__).parents[2] / "src/gobby/install/shared/workflows/pipelines/ask.yaml"
    return parse_ask_pipeline(yaml.safe_load(path.read_text()))


def test_bundled_ask_pipeline_uses_only_internal_stage_steps() -> None:
    pipeline = _bundled_pipeline()

    assert pipeline.resume_on_restart is True
    assert tuple(step.id for step in pipeline.steps) == tuple(ASK_PIPELINE_STEPS)
    assert all(step.mcp is not None for step in pipeline.steps)
    assert all(step.exec is None and step.prompt is None for step in pipeline.steps)
    assert {step.mcp.server for step in pipeline.steps if step.mcp} == {"gobby-ask"}


def test_bundled_ask_pipeline_arguments_match_registered_stage_schemas() -> None:
    pipeline = _bundled_pipeline()
    registry = create_ask_registry(
        lambda _project_id: None,
        project_root_resolver=lambda _project_id, _project_path: Path("/unused"),
    )
    mismatches: list[str] = []

    for step in pipeline.steps:
        assert step.mcp is not None
        schema = registry.get_schema(step.mcp.tool)
        assert schema is not None, step.mcp.tool
        input_schema = schema["inputSchema"]
        arguments = step.mcp.arguments
        assert arguments is not None, step.id
        argument_names = set(arguments)
        property_names = set(input_schema["properties"])
        required_names = set(input_schema["required"])
        unexpected = argument_names - property_names
        missing = required_names - argument_names
        if unexpected or missing:
            mismatches.append(
                f"{step.id}: unexpected={sorted(unexpected)}, missing={sorted(missing)}"
            )

    assert not mismatches, "\n".join(mismatches)
