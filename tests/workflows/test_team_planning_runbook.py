"""team-planning runbook launches the plan-enhancer seat alongside writer and adversary."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from gobby.workflows.definitions import PipelineDefinition
from gobby.workflows.pipeline.renderer import StepRenderer
from gobby.workflows.templates import TemplateEngine

PIPELINE = (
    Path(__file__).resolve().parents[2]
    / "src/gobby/install/shared/workflows/pipelines/team-planning.yaml"
)
SEAT_NAMES = ("plan-writer", "plan-adversary", "plan-enhancer")


def _definition() -> PipelineDefinition:
    return PipelineDefinition.model_validate(yaml.safe_load(PIPELINE.read_text()))


def _context(definition: PipelineDefinition, seats: str) -> dict[str, Any]:
    inputs = {name: spec.get("default") for name, spec in definition.inputs.items()}
    inputs.update(
        seats=seats,
        task="#25186",
        workspace="pilot-workspace",
        project_path="/pilot/shared-checkout",
        report_to="gobby#14972",
    )
    guard_output = {"tab_title": "Team Planning - #25186", "launch": seats.split(",")}
    return {
        "inputs": inputs,
        "steps": {"guard": {"output": guard_output}},
        "invocation_id": "pilot-run",
    }


def test_team_planning_launches_enhancer_seat() -> None:
    definition = _definition()
    assert [step.id for step in definition.steps] == ["guard", *SEAT_NAMES, "arrange"]
    assert "plan-enhancer" in definition.inputs["seats"]["description"]
    for name in ("provider", "model", "reasoning_effort"):
        assert f"plan_enhancer_{name}" in definition.inputs

    guard = definition.steps[0].mcp
    assert guard is not None and guard.arguments is not None
    catalogue = {entry["name"]: entry["agent"] for entry in guard.arguments["catalogue"]}
    assert catalogue == {seat: seat for seat in SEAT_NAMES}

    renderer = StepRenderer(TemplateEngine())
    context = _context(definition, seats="plan-enhancer,plan-adversary,plan-writer")
    launched: list[str] = []
    placements: dict[str, Any] = {}
    for step in definition.steps:
        if step.id not in SEAT_NAMES:
            continue
        assert renderer.should_run_step(step, context)
        assert step.mcp is not None and step.mcp.arguments is not None
        args = renderer.render_mcp_arguments(step.mcp.arguments, context, drop_none=True)
        assert (step.mcp.server, step.mcp.tool) == ("gobby-agents", "spawn_agent")
        assert args["agent"] == step.id
        assert args["checkout_mode"] == "none"
        assert args["project_path"] == "/pilot/shared-checkout"
        assert args["reserved_run_id"] == "pilot-run"
        placements[step.id] = args["placement"]
        context["steps"][step.id] = {"output": {"pane_ref": f"pilot:{step.id}"}}
        launched.append(step.id)

    # The writer opens the policy's Team Planning - #<task> tab; the others split into that tab.
    assert launched == list(SEAT_NAMES)
    assert placements["plan-writer"] == {
        "tab": {"workspace": "pilot-workspace", "title": "Team Planning - #25186"}
    }
    assert placements["plan-adversary"] == {
        "split": {"pane": "pilot:plan-writer", "axis": "right", "title": "Plan Adversary"}
    }
    assert placements["plan-enhancer"] == {
        "split": {"pane": "pilot:plan-writer", "axis": "right", "title": "Plan Enhancer"}
    }


def test_team_planning_relaunches_enhancer_only() -> None:
    definition = _definition()
    renderer = StepRenderer(TemplateEngine())
    context = _context(definition, seats="plan-enhancer")

    launched = [
        step.id
        for step in definition.steps
        if step.id in SEAT_NAMES and renderer.should_run_step(step, context)
    ]
    assert launched == ["plan-enhancer"]

    step = next(step for step in definition.steps if step.id == "plan-enhancer")
    assert step.mcp is not None and step.mcp.arguments is not None
    args = renderer.render_mcp_arguments(step.mcp.arguments, context, drop_none=True)
    assert args["agent"] == "plan-enhancer"
    assert args["placement"] == {
        "tab": {"workspace": "pilot-workspace", "title": "Team Planning - #25186"}
    }
