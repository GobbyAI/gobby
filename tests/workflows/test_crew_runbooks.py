"""Render the approved crew-lane seats without launching live agents."""

from pathlib import Path
from typing import Any

import pytest
import yaml

from gobby.workflows.definitions import PipelineDefinition
from gobby.workflows.pipeline.renderer import StepRenderer
from gobby.workflows.templates import TemplateEngine

ROOT = Path(__file__).resolve().parents[2]
PIPELINES = ROOT / ".gobby/workflows/pipelines"


def test_crew_lane_contains_only_approved_seats() -> None:
    paths = sorted(PIPELINES.glob("crew-*.yaml"))
    assert [path.stem for path in paths] == ["crew-lane"]
    definition = PipelineDefinition.model_validate(yaml.safe_load(paths[0].read_text()))
    assert definition.tags == ["runbook"]
    assert definition.resume_on_restart
    assert [step.id for step in definition.steps] == [
        "guard",
        "developer",
        "code-reviewer",
        "researcher",
        "lane-manager",
    ]
    guard = definition.steps[0].mcp
    assert guard is not None and guard.tool == "check_runbook_seats"
    assert guard.arguments is not None
    assert guard.arguments["catalogue"] == [
        {
            "name": "developer",
            "title": "${{ 'Lane ' + inputs.lane + ' developer' }}",
            "agent": "developer",
        },
        {
            "name": "code-reviewer",
            "title": "${{ 'Lane ' + inputs.lane + ' code reviewer' }}",
            "agent": "code-reviewer",
        },
        {
            "name": "researcher",
            "title": "${{ 'Lane ' + inputs.lane + ' researcher' }}",
            "agent": "researcher",
        },
        {
            "name": "lane-manager",
            "title": "${{ 'Lane ' + inputs.lane + ' manager' }}",
            "agent": "lane-manager",
        },
    ]
    assert definition.inputs["seats"]["default"] == "developer"
    assert definition.inputs["workspace"]["required"]
    assert definition.inputs["worktree_id"]["required"]
    assert [
        definition.inputs[f"reviewer_{name}"]["default"]
        for name in ("provider", "model", "reasoning_effort", "role_file")
    ] == ["claude", "claude-opus-5-5", "xhigh", "code-reviewer.md"]
    assert [
        definition.inputs[f"researcher_{name}"]["default"]
        for name in ("provider", "model", "reasoning_effort", "role_file")
    ] == ["codex", "gpt-6.1-sol", "medium", "researcher.md"]
    assert [
        definition.inputs[f"manager_{name}"]["default"]
        for name in ("provider", "model", "reasoning_effort", "role_file")
    ] == ["claude", "claude-sonnet-5-5", "medium", "lane-manager.md"]


@pytest.mark.parametrize(
    ("seat", "prefix", "title", "lane", "provider", "model", "effort", "role_file"),
    [
        (
            "developer",
            "developer",
            "developer",
            "3",
            "codex",
            "gpt-6.1-sol",
            "medium",
            "lane-4-runbooks.md",
        ),
        (
            "developer",
            "developer",
            "developer",
            "6",
            "claude",
            "claude-opus-5-5",
            "xhigh",
            "lane-6-everything-else.md",
        ),
        (
            "code-reviewer",
            "reviewer",
            "code reviewer",
            "3",
            "claude",
            "claude-opus-5-5",
            "xhigh",
            "code-reviewer.md",
        ),
        (
            "code-reviewer",
            "reviewer",
            "code reviewer",
            "6",
            "codex",
            "gpt-6.1-sol",
            "high",
            "pilot-reviewer.md",
        ),
        (
            "researcher",
            "researcher",
            "researcher",
            "3",
            "codex",
            "gpt-6.1-sol",
            "medium",
            "researcher.md",
        ),
        (
            "researcher",
            "researcher",
            "researcher",
            "6",
            "claude",
            "claude-opus-5-5",
            "high",
            "pilot-researcher.md",
        ),
        (
            "lane-manager",
            "manager",
            "manager",
            "3",
            "claude",
            "claude-sonnet-5-5",
            "medium",
            "lane-manager.md",
        ),
        (
            "lane-manager",
            "manager",
            "manager",
            "6",
            "claude",
            "claude-opus-5-5",
            "high",
            "pilot-manager.md",
        ),
    ],
)
def test_crew_lane_renders_operator_inputs(
    seat: str,
    prefix: str,
    title: str,
    lane: str,
    provider: str,
    model: str,
    effort: str,
    role_file: str,
) -> None:
    definition = PipelineDefinition.model_validate(
        yaml.safe_load((PIPELINES / "crew-lane.yaml").read_text())
    )
    inputs = {name: spec.get("default") for name, spec in definition.inputs.items()}
    inputs.update(
        lane=lane,
        seats=seat,
        workspace="pilot-workspace",
        worktree_id="pilot-worktree",
        report_to="gobby#14972",
    )
    inputs.update(
        {
            f"{prefix}_provider": provider,
            f"{prefix}_model": model,
            f"{prefix}_reasoning_effort": effort,
            f"{prefix}_role_file": role_file,
        }
    )
    context: dict[str, Any] = {"inputs": inputs, "steps": {}, "invocation_id": "pilot-run"}
    renderer = StepRenderer(TemplateEngine())
    guard = definition.steps[0].mcp
    assert guard is not None and guard.arguments is not None
    guarded = renderer.render_mcp_arguments(guard.arguments, context, drop_none=True)
    assert guarded["workspace"] == "pilot-workspace"
    assert guarded["requested"] == seat
    catalogued = next(entry for entry in guarded["catalogue"] if entry["name"] == seat)
    assert catalogued["title"] == f"Lane {lane} {title}"
    assert catalogued["agent"] == seat
    step = next(step for step in definition.steps if step.id == seat)
    assert renderer.should_run_step(step, context)
    assert step.mcp is not None and step.mcp.arguments is not None
    args = renderer.render_mcp_arguments(step.mcp.arguments, context, drop_none=True)
    assert args["agent"] == seat
    assert (args["provider"], args["model"], args["reasoning_effort"]) == (provider, model, effort)
    assert args["execution_mode"] == "interactive"
    assert args["checkout_mode"] == "none"
    assert args["worktree_id"] == "pilot-worktree" and "project_path" not in args
    assert args["reserved_run_id"] == "pilot-run"
    assert args["placement"] == {
        "tab": {"workspace": "pilot-workspace", "title": f"Lane {lane} {title}"}
    }
    assert f".gobby/roles/{role_file}" in args["prompt"]
    assert ".gobby/roles/_common.md first" in args["prompt"]
    assert f"Explicit lane assignment: Lane {lane}" in args["prompt"]
    assert 'target="session", target_id="gobby#14972"' in args["prompt"]
    assert 'owner_session="gobby#14972"' in args["prompt"]
    assert definition.inputs["report_to"]["required"]
    inputs["seats"] = "unapproved-seat"
    assert not renderer.should_run_step(step, context)


@pytest.mark.parametrize(
    ("requested", "expected"),
    [
        ("developer", ["developer"]),
        ("researcher", ["researcher"]),
        ("code-reviewer,researcher", ["code-reviewer", "researcher"]),
        ("code-reviewer,lane-manager", ["code-reviewer", "lane-manager"]),
        ("developer,code-reviewer,lane-manager", ["developer", "code-reviewer", "lane-manager"]),
        (
            "developer,code-reviewer,researcher,lane-manager",
            ["developer", "code-reviewer", "researcher", "lane-manager"],
        ),
    ],
)
def test_crew_lane_selects_only_requested_seats(requested: str, expected: list[str]) -> None:
    definition = PipelineDefinition.model_validate(
        yaml.safe_load((PIPELINES / "crew-lane.yaml").read_text())
    )
    context = {"inputs": {"seats": requested}}
    renderer = StepRenderer(TemplateEngine())

    assert [
        step.id for step in definition.steps[1:] if renderer.should_run_step(step, context)
    ] == expected
