"""Render the approved first crew-lane step without launching live agents."""

from pathlib import Path
from typing import Any

import pytest
import yaml

from gobby.workflows.definitions import PipelineDefinition
from gobby.workflows.pipeline.renderer import StepRenderer
from gobby.workflows.templates import TemplateEngine

ROOT = Path(__file__).resolve().parents[2]
PIPELINES = ROOT / ".gobby/workflows/pipelines"


def test_crew_lane_contains_only_approved_developer_step() -> None:
    paths = sorted(PIPELINES.glob("crew-*.yaml"))
    assert [path.stem for path in paths] == ["crew-lane"]
    definition = PipelineDefinition.model_validate(yaml.safe_load(paths[0].read_text()))
    assert definition.tags == ["runbook"]
    assert definition.resume_on_restart
    assert [step.id for step in definition.steps] == ["guard", "developer"]
    guard = definition.steps[0].mcp
    assert guard is not None and guard.tool == "check_runbook_seats"
    assert guard.arguments is not None
    assert guard.arguments["catalogue"] == [
        {
            "name": "developer",
            "title": "${{ 'Lane ' + inputs.lane + ' developer' }}",
            "agent": "developer",
        }
    ]
    assert definition.inputs["seats"]["default"] == "developer"
    assert definition.inputs["workspace"]["required"]
    assert definition.inputs["worktree_id"]["required"]


@pytest.mark.parametrize(
    ("lane", "provider", "model", "effort", "role_file"),
    [
        ("3", "codex", "gpt-6.1-sol", "medium", "lane-4-runbooks.md"),
        ("6", "claude", "claude-opus-5-5", "xhigh", "lane-6-everything-else.md"),
    ],
)
def test_crew_lane_renders_operator_inputs(
    lane: str, provider: str, model: str, effort: str, role_file: str
) -> None:
    definition = PipelineDefinition.model_validate(
        yaml.safe_load((PIPELINES / "crew-lane.yaml").read_text())
    )
    inputs = {name: spec.get("default") for name, spec in definition.inputs.items()}
    inputs.update(
        lane=lane,
        workspace="pilot-workspace",
        worktree_id="pilot-worktree",
        developer_provider=provider,
        developer_model=model,
        developer_reasoning_effort=effort,
        developer_role_file=role_file,
    )
    context: dict[str, Any] = {"inputs": inputs, "steps": {}, "invocation_id": "pilot-run"}
    renderer = StepRenderer(TemplateEngine())
    guard = definition.steps[0].mcp
    assert guard is not None and guard.arguments is not None
    guarded = renderer.render_mcp_arguments(guard.arguments, context, drop_none=True)
    assert guarded["workspace"] == "pilot-workspace"
    assert guarded["requested"] == "developer"
    assert guarded["catalogue"][0]["title"] == f"Lane {lane} developer"
    step = definition.steps[1]
    assert renderer.should_run_step(step, context)
    assert step.mcp is not None and step.mcp.arguments is not None
    args = renderer.render_mcp_arguments(step.mcp.arguments, context, drop_none=True)
    assert args["agent"] == "developer"
    assert (args["provider"], args["model"], args["reasoning_effort"]) == (provider, model, effort)
    assert args["execution_mode"] == "interactive"
    assert args["checkout_mode"] == "none"
    assert args["worktree_id"] == "pilot-worktree" and "project_path" not in args
    assert args["reserved_run_id"] == "pilot-run"
    assert args["placement"] == {
        "tab": {"workspace": "pilot-workspace", "title": f"Lane {lane} developer"}
    }
    assert f".gobby/roles/{role_file}" in args["prompt"]
    assert ".gobby/roles/_common.md first" in args["prompt"]
    assert f"Explicit lane assignment: Lane {lane}" in args["prompt"]
    inputs["seats"] = "code-reviewer"
    assert not renderer.should_run_step(step, context)
