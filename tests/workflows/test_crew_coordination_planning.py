"""Exercise the project runbooks through the pipeline schema and renderer."""

from itertools import combinations
from pathlib import Path
from typing import Any

import pytest
import yaml

from gobby.workflows.definitions import PipelineDefinition
from gobby.workflows.pipeline.renderer import StepRenderer
from gobby.workflows.templates import TemplateEngine

PIPELINES = Path(__file__).resolve().parents[2] / ".gobby/workflows/pipelines"
SEATS = {
    "crew-coordination": (
        "assistant",
        "orchestrator",
        "archivist",
        "log-monitor",
        "merge-manager",
        "inbox-manager",
    ),
    "crew-planning": ("plan-writer", "plan-adversary"),
}
ROLE_FILES = {seat: f"{seat}.md" for seats in SEATS.values() for seat in seats}
ROLE_FILES["log-monitor"] = "monitor.md"


@pytest.mark.parametrize("pipeline", SEATS)
def test_crew_runbook_guards_only_approved_seats(pipeline: str) -> None:
    definition = PipelineDefinition.model_validate(
        yaml.safe_load((PIPELINES / f"{pipeline}.yaml").read_text())
    )
    assert definition.name == pipeline
    assert definition.tags == ["runbook"]
    assert definition.resume_on_restart
    assert [step.id for step in definition.steps] == ["guard", *SEATS[pipeline]]
    assert definition.inputs["seats"]["default"] == SEATS[pipeline][0]
    assert definition.inputs["workspace"]["required"]
    assert definition.inputs["project_path"]["required"]
    assert definition.inputs["report_to"]["required"]
    assert definition.inputs["lane_pane"]["default"] is None
    assert "worktree_id" not in definition.inputs
    guard = definition.steps[0].mcp
    assert guard is not None
    assert (guard.server, guard.tool) == ("gobby-agents", "check_runbook_seats")
    assert guard.arguments is not None
    assert guard.arguments["require_report_to"] is True
    assert [(entry["name"], entry["agent"]) for entry in guard.arguments["catalogue"]] == [
        (seat, seat) for seat in SEATS[pipeline]
    ]


@pytest.mark.parametrize("lane_pane", [None, "pilot:existing-pane"])
@pytest.mark.parametrize(
    ("pipeline", "seats"),
    [
        (pipeline, subset)
        for pipeline, catalogue in SEATS.items()
        for size in range(1, len(catalogue) + 1)
        for subset in combinations(catalogue, size)
    ],
)
def test_crew_runbook_renders_each_subset_in_one_tab(
    pipeline: str, seats: tuple[str, ...], lane_pane: str | None
) -> None:
    definition = PipelineDefinition.model_validate(
        yaml.safe_load((PIPELINES / f"{pipeline}.yaml").read_text())
    )
    inputs = {name: spec.get("default") for name, spec in definition.inputs.items()}
    inputs.update(
        lane="7",
        seats=",".join(reversed(seats)),
        workspace="pilot-workspace",
        project_path="/pilot/shared-checkout",
        lane_pane=lane_pane,
        report_to="gobby#14972",
    )
    context: dict[str, Any] = {"inputs": inputs, "steps": {}, "invocation_id": "pilot-run"}
    renderer = StepRenderer(TemplateEngine())
    guard = definition.steps[0].mcp
    assert guard is not None and guard.arguments is not None
    guarded = renderer.render_mcp_arguments(guard.arguments, context, drop_none=True)
    assert guarded["workspace"] == "pilot-workspace"
    assert guarded["requested"] == inputs["seats"]
    assert guarded["report_to"] == "gobby#14972"
    catalogue = {entry["name"]: entry for entry in guarded["catalogue"]}
    anchor = lane_pane
    launched = []
    for step in definition.steps[1:]:
        if not renderer.should_run_step(step, context):
            continue
        assert step.mcp is not None and step.mcp.arguments is not None
        assert (step.mcp.server, step.mcp.tool) == ("gobby-agents", "spawn_agent")
        args = renderer.render_mcp_arguments(step.mcp.arguments, context, drop_none=True)
        assert args["agent"] == step.id
        assert args["provider"] in {"claude", "codex"}
        assert isinstance(args["model"], str) and args["model"]
        assert isinstance(args["reasoning_effort"], str) and args["reasoning_effort"]
        assert args["execution_mode"] == "interactive"
        assert args["checkout_mode"] == "none"
        assert args["project_path"] == "/pilot/shared-checkout"
        assert "worktree_id" not in args
        assert args["reserved_run_id"] == "pilot-run"
        assert f".gobby/roles/{ROLE_FILES[step.id]}" in args["prompt"]
        assert ".gobby/roles/_common.md first" in args["prompt"]
        assert 'target="session", target_id="gobby#14972"' in args["prompt"]
        assert 'owner_session="gobby#14972"' in args["prompt"]
        assert "every SRT denial" in args["prompt"]
        assert "max_active_agents" in args["prompt"]
        assert "current session ref" in args["prompt"]
        if anchor is None:
            assert args["placement"] == {"tab": {"workspace": "pilot-workspace", "title": "Lane 7"}}
            anchor = f"pilot:{step.id}"
        else:
            assert args["placement"] == {
                "split": {"pane": anchor, "axis": "right", "title": catalogue[step.id]["title"]}
            }
        context["steps"][step.id] = {"output": {"pane_ref": f"pilot:{step.id}"}}
        launched.append(step.id)
    assert launched == list(seats)


@pytest.mark.parametrize(
    ("pipeline", "seat"), [(pipeline, seat) for pipeline, seats in SEATS.items() for seat in seats]
)
def test_crew_runbook_preserves_operator_seat_overrides(pipeline: str, seat: str) -> None:
    definition = PipelineDefinition.model_validate(
        yaml.safe_load((PIPELINES / f"{pipeline}.yaml").read_text())
    )
    inputs = {name: spec.get("default") for name, spec in definition.inputs.items()}
    prefix = seat.replace("-", "_")
    inputs.update(
        seats=seat,
        workspace="pilot-workspace",
        project_path="/pilot/shared-checkout",
        report_to="gobby#14972",
    )
    inputs.update(
        {
            f"{prefix}_provider": "codex",
            f"{prefix}_model": "gpt-6.1-sol",
            f"{prefix}_reasoning_effort": "high",
            f"{prefix}_role_file": "pilot-role.md",
        }
    )
    context: dict[str, Any] = {"inputs": inputs, "steps": {}, "invocation_id": "pilot-run"}
    renderer = StepRenderer(TemplateEngine())
    step = next(step for step in definition.steps if step.id == seat)
    assert renderer.should_run_step(step, context)
    assert step.mcp is not None and step.mcp.arguments is not None
    args = renderer.render_mcp_arguments(step.mcp.arguments, context, drop_none=True)
    assert (args["provider"], args["model"], args["reasoning_effort"]) == (
        "codex",
        "gpt-6.1-sol",
        "high",
    )
    assert ".gobby/roles/pilot-role.md" in args["prompt"]
    inputs["seats"] = "unapproved-seat"
    assert not renderer.should_run_step(step, context)
