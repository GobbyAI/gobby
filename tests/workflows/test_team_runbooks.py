"""Render the approved team-lane seats without launching live agents."""

from itertools import combinations
from pathlib import Path
from typing import Any

import pytest
import yaml

from gobby.storage.hub.protocol import HubDatabase
from gobby.workflows.definitions import PipelineDefinition
from gobby.workflows.pipeline.renderer import StepRenderer
from gobby.workflows.templates import TemplateEngine
from tests.workflows.test_seat_step_loops import SeatRun

ROOT = Path(__file__).resolve().parents[2]
PIPELINES = ROOT / "src/gobby/install/shared/workflows/pipelines"
ROLE_NAMES = {
    "developer": "developer",
    "code-reviewer": "code reviewer",
    "researcher": "researcher",
    "lane-manager": "lane manager",
}


@pytest.mark.parametrize(
    ("seat", "ready_step"), [("developer", "claim"), ("code-reviewer", "await")]
)
async def test_team_lane_requires_code_review_before_seat_work(
    temp_db: HubDatabase, seat: str, ready_step: str
) -> None:
    definition = PipelineDefinition.model_validate(
        yaml.safe_load((PIPELINES / "team-lane.yaml").read_text())
    )
    step = next(step for step in definition.steps if step.id == seat)
    assert step.mcp is not None and step.mcp.arguments is not None
    run = SeatRun(temp_db, step.mcp.arguments["agent"])
    assert run.step == "load_skills"

    # Every other bootstrap load must leave review/implementation unavailable.
    await run.load(*(entry for entry in run.vars["required_skills"] if entry != "code-review"))
    assert run.step == "load_skills"
    assert not await run.admits("gobby-tasks", "record_close_receipt")

    await run.load("code-review")
    assert run.step == ready_step
    assert "code-review" in run.session_vars["loaded_skills"]
    if seat == "code-reviewer":
        await run.set_flag("candidate_task", "#101")
        await run.set_flag("candidate_received", True)
        assert run.step == "review"
        await run.set_flag("verdict_ready", True)
        assert await run.admits("gobby-tasks", "record_close_receipt")


def _render_context(definition: PipelineDefinition, inputs: dict[str, Any]) -> dict[str, Any]:
    """Build the render context the executor builds: declared string inputs keep their text."""
    string_inputs = frozenset(
        name for name, spec in definition.inputs.items() if spec.get("type") == "string"
    )
    return {
        "inputs": inputs,
        "steps": {},
        "invocation_id": "pilot-run",
        "_string_inputs": string_inputs,
    }


def test_team_lane_contains_only_approved_seats() -> None:
    paths = sorted(PIPELINES.glob("team-*.yaml"))
    assert [path.stem for path in paths] == ["team-coordination", "team-lane", "team-planning"]
    definition = PipelineDefinition.model_validate(
        yaml.safe_load((PIPELINES / "team-lane.yaml").read_text())
    )
    assert definition.name == "team-lane"
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
    assert definition.inputs["lane"]["required"] is True
    assert definition.inputs["seats"]["default"] == "developer"
    for name in ("workspace", "lane_pane", "worktree", "report_to", "lane_columns", "lane_rows"):
        assert definition.inputs[name]["default"] is None
        assert not definition.inputs[name].get("required", False)
    assert set(definition.inputs) == {
        "lane_columns",
        "lane_rows",
        "lane",
        "workspace",
        "lane_pane",
        "worktree",
        "seats",
        "report_to",
        "developer_provider",
        "developer_model",
        "developer_reasoning_effort",
        "reviewer_provider",
        "reviewer_model",
        "reviewer_reasoning_effort",
        "researcher_provider",
        "researcher_model",
        "researcher_reasoning_effort",
        "manager_provider",
        "manager_model",
        "manager_reasoning_effort",
    }
    assert [
        definition.inputs[f"reviewer_{name}"]["default"]
        for name in ("provider", "model", "reasoning_effort")
    ] == ["claude", "claude-opus-5-5", "xhigh"]
    assert [
        definition.inputs[f"researcher_{name}"]["default"]
        for name in ("provider", "model", "reasoning_effort")
    ] == ["codex", "gpt-6.1-sol", "medium"]
    assert [
        definition.inputs[f"manager_{name}"]["default"]
        for name in ("provider", "model", "reasoning_effort")
    ] == ["claude", "claude-sonnet-5-5", "medium"]


def test_team_lane_passes_lane_to_every_seat() -> None:
    definition = PipelineDefinition.model_validate(
        yaml.safe_load((PIPELINES / "team-lane.yaml").read_text())
    )
    seat_steps = definition.steps[1:]
    assert [step.id for step in seat_steps] == [
        "developer",
        "code-reviewer",
        "researcher",
        "lane-manager",
    ]
    for step in seat_steps:
        assert step.mcp is not None and step.mcp.arguments is not None
        assert (step.mcp.server, step.mcp.tool) == ("gobby-agents", "spawn_agent")
        assert step.mcp.arguments["lane"] == "${{ inputs.lane }}"


@pytest.mark.parametrize(
    ("seat", "prefix", "title", "lane", "provider", "model", "effort"),
    [
        ("developer", "developer", "developer", "3", "codex", "gpt-6.1-sol", "medium"),
        ("developer", "developer", "developer", "6", "claude", "claude-opus-5-5", "xhigh"),
        ("code-reviewer", "reviewer", "code reviewer", "3", "claude", "claude-opus-5-5", "xhigh"),
        ("code-reviewer", "reviewer", "code reviewer", "6", "codex", "gpt-6.1-sol", "high"),
        ("researcher", "researcher", "researcher", "3", "codex", "gpt-6.1-sol", "medium"),
        ("researcher", "researcher", "researcher", "6", "claude", "claude-opus-5-5", "high"),
        ("lane-manager", "manager", "manager", "3", "claude", "claude-sonnet-5-5", "medium"),
        ("lane-manager", "manager", "manager", "6", "claude", "claude-opus-5-5", "high"),
    ],
)
def test_team_lane_renders_operator_inputs(
    seat: str,
    prefix: str,
    title: str,
    lane: str,
    provider: str,
    model: str,
    effort: str,
) -> None:
    definition = PipelineDefinition.model_validate(
        yaml.safe_load((PIPELINES / "team-lane.yaml").read_text())
    )
    inputs = {name: spec.get("default") for name, spec in definition.inputs.items()}
    inputs.update(
        lane=lane,
        lane_columns=160,
        lane_rows=100,
        seats=seat,
        workspace="pilot-workspace",
        worktree="pilot-worktree",
        report_to="gobby#14972",
    )
    inputs.update(
        {
            f"{prefix}_provider": provider,
            f"{prefix}_model": model,
            f"{prefix}_reasoning_effort": effort,
        }
    )
    context = _render_context(definition, inputs)
    renderer = StepRenderer(TemplateEngine())
    guard = definition.steps[0].mcp
    assert guard is not None and guard.arguments is not None
    guarded = renderer.render_mcp_arguments(guard.arguments, context, drop_none=True)
    assert guarded["lane"] == lane
    assert guarded["workspace"] == "pilot-workspace"
    assert guarded["requested"] == seat
    catalogued = next(entry for entry in guarded["catalogue"] if entry["name"] == seat)
    assert catalogued["title"] == f"Lane {lane} {title}"
    assert catalogued["agent"] == seat
    assert guarded["worktree"] == "pilot-worktree"
    assert (guarded["lane_columns"], guarded["lane_rows"]) == (160, 100)
    context["steps"]["guard"] = {
        "output": {
            "workspace": "pilot-workspace",
            "lane_pane": "pilot:resolved-pane",
            "worktree": "pilot-worktree",
            "report_to": "gobby#14972",
            "project_path": "/pilot/project",
            "lane_columns": 160,
            "lane_rows": 100,
        }
    }
    step = next(step for step in definition.steps if step.id == seat)
    assert renderer.should_run_step(step, context)
    assert step.mcp is not None and step.mcp.arguments is not None
    args = renderer.render_mcp_arguments(step.mcp.arguments, context, drop_none=True)
    assert args["agent"] == seat
    assert args["lane"] == lane
    assert (args["provider"], args["model"], args["reasoning_effort"]) == (provider, model, effort)
    assert args["execution_mode"] == "interactive"
    assert args["checkout_mode"] == "none"
    if seat == "developer":
        assert args["worktree_id"] == "pilot-worktree" and "project_path" not in args
    else:
        assert args["project_path"] == "/pilot/project" and "worktree_id" not in args
    assert args["reserved_run_id"] == "pilot-run"
    assert args["placement"] == {
        "split": {
            "pane": "pilot:resolved-pane",
            "axis": "balanced",
            "columns": 160,
            "rows": 100,
            "title": f"Lane {lane} {title}",
        }
    }
    assert args["prompt"].startswith(f"You are the {ROLE_NAMES[seat]} seat for lane {lane}.")
    assert "SRT denial" in args["prompt"]
    assert ".gobby/" not in args["prompt"]
    assert "installed agent definition" in args["prompt"]
    assert 'target="session", target_id="gobby#14972"' in args["prompt"]
    assert 'owner_session="gobby#14972"' in args["prompt"]
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
def test_team_lane_selects_only_requested_seats(requested: str, expected: list[str]) -> None:
    definition = PipelineDefinition.model_validate(
        yaml.safe_load((PIPELINES / "team-lane.yaml").read_text())
    )
    context = {"inputs": {"seats": requested}}
    renderer = StepRenderer(TemplateEngine())

    assert [
        step.id for step in definition.steps[1:] if renderer.should_run_step(step, context)
    ] == expected


@pytest.mark.parametrize("lane_pane", [None, "pilot:existing-pane"])
@pytest.mark.parametrize(
    "seats",
    [
        subset
        for size in range(1, 5)
        for subset in combinations(
            ("developer", "code-reviewer", "researcher", "lane-manager"), size
        )
    ],
)
def test_team_lane_keeps_selected_seats_in_one_tab(
    seats: tuple[str, ...], lane_pane: str | None
) -> None:
    definition = PipelineDefinition.model_validate(
        yaml.safe_load((PIPELINES / "team-lane.yaml").read_text())
    )
    inputs = {name: spec.get("default") for name, spec in definition.inputs.items()}
    inputs.update(
        lane="6",
        seats=",".join(reversed(seats)),
        lane_pane=lane_pane,
        workspace="pilot-workspace",
        worktree="pilot-worktree",
        report_to="gobby#14972",
    )
    context = _render_context(definition, inputs)
    renderer = StepRenderer(TemplateEngine())
    anchor_pane = lane_pane or "pilot:inferred-pane"
    context["steps"]["guard"] = {
        "output": {
            "workspace": "pilot-workspace",
            "lane_pane": anchor_pane,
            "worktree": "pilot-worktree",
            "report_to": "gobby#14972",
            "project_path": "/pilot/project",
            "lane_columns": 205,
            "lane_rows": 59,
        }
    }
    titles = {
        "developer": "developer",
        "code-reviewer": "code reviewer",
        "researcher": "researcher",
        "lane-manager": "manager",
    }
    placements = []
    for step in definition.steps[1:]:
        if not renderer.should_run_step(step, context):
            continue
        assert step.mcp is not None and step.mcp.arguments is not None
        args = renderer.render_mcp_arguments(step.mcp.arguments, context, drop_none=True)
        assert args["lane"] == "6"
        placement = args["placement"]
        placements.append(placement)
        assert placement == {
            "split": {
                "pane": anchor_pane,
                "axis": "balanced",
                "columns": 205,
                "rows": 59,
                "title": f"Lane 6 {titles[step.id]}",
            }
        }
        context["steps"][step.id] = {"output": {"pane_ref": f"pilot:{step.id}"}}
    assert len(placements) == len(seats)
    assert all("split" in placement for placement in placements)
