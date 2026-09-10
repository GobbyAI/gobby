from __future__ import annotations

from pathlib import Path

import yaml

from gobby.ask.pipeline import ASK_PIPELINE_STEPS, parse_ask_pipeline


def test_bundled_ask_pipeline_uses_only_internal_stage_steps() -> None:
    path = (
        Path(__file__).parents[2]
        / "src/gobby/install/shared/workflows/pipelines/ask.yaml"
    )
    pipeline = parse_ask_pipeline(yaml.safe_load(path.read_text()))

    assert pipeline.resume_on_restart is True
    assert tuple(step.id for step in pipeline.steps) == tuple(ASK_PIPELINE_STEPS)
    assert all(step.mcp is not None for step in pipeline.steps)
    assert all(step.exec is None and step.prompt is None for step in pipeline.steps)
    assert {step.mcp.server for step in pipeline.steps if step.mcp} == {"gobby-ask"}
