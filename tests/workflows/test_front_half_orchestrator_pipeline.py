"""Content tests for front-half orchestrator planner dispatch wiring."""

from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

PIPELINE_PATH = Path("src/gobby/install/shared/workflows/pipelines/front-half-orchestrator.yaml")


def test_dispatch_planner_passes_artifact_path_initial_variable() -> None:
    with PIPELINE_PATH.open() as f:
        data = yaml.safe_load(f)

    dispatch_planner = next(step for step in data["steps"] if step["id"] == "dispatch_planner")
    arguments = dispatch_planner["mcp"]["arguments"]
    assert (
        arguments["initial_variables"]["artifact_path"]
        == "${{ steps.tick.output.artifacts.plan_file }}"
    )
