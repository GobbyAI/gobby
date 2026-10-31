"""Exercise the team planning runbook through the schema and renderer."""

import re
from itertools import combinations
from pathlib import Path
from typing import Any

import pytest
import yaml

from gobby.workflows.definitions import PipelineDefinition
from gobby.workflows.pipeline.renderer import StepRenderer
from gobby.workflows.templates import TemplateEngine

PIPELINES = Path(__file__).resolve().parents[2] / "src/gobby/install/shared/workflows/pipelines"
SEATS = {"team-planning": ("plan-writer", "plan-adversary", "plan-enhancer")}
SHARED_INPUTS = {"workspace", "task", "seat_pane", "project_path", "seats", "report_to"}
STEP_REFERENCE = re.compile(r"steps\['([^']+)'\]")
STEP_ORDER = {
    "team-planning": (
        "guard",
        "target",
        "planning-children-count",
        "planning-children",
        "planning-child",
        "plan-writer",
        "plan-adversary",
        "plan-enhancer",
        "notify-writer",
        "arrange",
    ),
}


@pytest.mark.parametrize("pipeline", SEATS)
def test_team_runbook_guards_only_approved_seats(pipeline: str) -> None:
    definition = PipelineDefinition.model_validate(
        yaml.safe_load((PIPELINES / f"{pipeline}.yaml").read_text())
    )
    assert definition.name == pipeline
    assert definition.tags == ["runbook"]
    assert definition.resume_on_restart
    assert [step.id for step in definition.steps] == list(STEP_ORDER[pipeline])
    assert definition.inputs["seats"]["default"] == SEATS[pipeline][0]
    assert definition.inputs["workspace"]["required"]
    assert definition.inputs["project_path"]["required"]
    assert definition.inputs["report_to"]["required"]
    assert definition.inputs["seat_pane"]["default"] is None
    seat_inputs = {
        f"{seat.replace('-', '_')}_{name}"
        for seat in SEATS[pipeline]
        for name in ("provider", "model", "reasoning_effort")
    }
    assert set(definition.inputs) == SHARED_INPUTS | seat_inputs
    guard = definition.steps[0].mcp
    assert guard is not None
    assert (guard.server, guard.tool) == ("gobby-agents", "check_runbook_seats")
    assert guard.arguments is not None
    assert guard.arguments["require_report_to"] is True
    assert [(entry["name"], entry["agent"]) for entry in guard.arguments["catalogue"]] == [
        (seat, seat) for seat in SEATS[pipeline]
    ]


@pytest.mark.parametrize("pipeline", SEATS)
def test_team_placements_reference_only_defined_steps(pipeline: str) -> None:
    text = (PIPELINES / f"{pipeline}.yaml").read_text()
    definition = PipelineDefinition.model_validate(yaml.safe_load(text))
    step_ids = {step.id for step in definition.steps}
    assert set(STEP_REFERENCE.findall(text)) <= step_ids


@pytest.mark.parametrize("pipeline", ["devteam", "support", "team-planning"])
def test_team_pipelines_carry_no_retired_seats(pipeline: str) -> None:
    definition = PipelineDefinition.model_validate(
        yaml.safe_load((PIPELINES / f"{pipeline}.yaml").read_text())
    )
    launched = {
        step.mcp.arguments["agent"]
        for step in definition.steps
        if step.mcp and step.mcp.tool == "spawn_agent" and step.mcp.arguments
    }
    for retired in ("assistant", "orchestrator", "merge-manager", "merge_manager"):
        assert retired not in launched, retired
        assert not any(name.startswith(f"{retired}_") for name in definition.inputs), retired


def _assert_support_step(
    step_id: str, mcp: Any, args: dict[str, Any], seats: tuple[str, ...], context: dict[str, Any]
) -> None:
    """Check a non-seat step's contract and record the output later steps read."""
    if step_id == "target":
        assert (mcp.server, mcp.tool) == ("gobby-tasks", "get_task")
        assert args == {"task_id": "#25186"}
        context["steps"][step_id] = {"output": {"title": "Planning runbook"}}
    elif step_id == "planning-children-count":
        assert (mcp.server, mcp.tool) == ("gobby-tasks", "list_tasks")
        assert args == {"parent_task_id": "#25186", "closed": False, "limit": 1}
        context["steps"][step_id] = {"output": {"open_count": 0, "tasks": []}}
    elif step_id == "planning-children":
        assert (mcp.server, mcp.tool) == ("gobby-tasks", "list_tasks")
        assert args == {"parent_task_id": "#25186", "closed": False, "limit": 1}
        context["steps"][step_id] = {"output": {"tasks": []}}
    elif step_id == "planning-child":
        assert (mcp.server, mcp.tool) == ("gobby-tasks", "create_task")
        assert args["title"] == "Plan Planning runbook"
        assert args["parent_task_id"] == "#25186"
        assert args["category"] == "planning"
        context["steps"][step_id] = {"output": {"ref": "#25400"}}
    else:
        assert step_id == "notify-writer"
        assert (mcp.server, mcp.tool) == ("gobby-agents", "send_message")
        assert args["target"] == "session"
        assert args["target_id"] == "plan-writer-session"
        for seat in seats:
            if seat != "plan-writer":
                assert f"session {seat}-session" in args["content"]


@pytest.mark.parametrize("seat_pane", [None, "pilot:existing-pane"])
@pytest.mark.parametrize(
    ("pipeline", "seats"),
    [
        (pipeline, subset)
        for pipeline, catalogue in SEATS.items()
        for size in range(1, len(catalogue) + 1)
        for subset in combinations(catalogue, size)
    ],
)
def test_team_runbook_renders_each_subset_in_one_tab(
    pipeline: str, seats: tuple[str, ...], seat_pane: str | None
) -> None:
    definition = PipelineDefinition.model_validate(
        yaml.safe_load((PIPELINES / f"{pipeline}.yaml").read_text())
    )
    inputs = {name: spec.get("default") for name, spec in definition.inputs.items()}
    inputs.update(
        seats=",".join(reversed(seats)),
        task="#25186",
        workspace="pilot-workspace",
        project_path="/pilot/shared-checkout",
        seat_pane=seat_pane,
        report_to="gobby#14972",
    )
    guard_output = {
        "tab_title": "Team Planning - #25186",
        "tab_exists": False,
        "launch": list(seats),
    }
    context: dict[str, Any] = {
        "inputs": inputs,
        "steps": {"guard": {"output": guard_output}},
        "invocation_id": "pilot-run",
    }
    renderer = StepRenderer(TemplateEngine())
    guard = definition.steps[0].mcp
    assert guard is not None and guard.arguments is not None
    guarded = renderer.render_mcp_arguments(guard.arguments, context, drop_none=True)
    assert guarded["workspace"] == "pilot-workspace"
    assert guarded["requested"] == inputs["seats"]
    assert guarded["report_to"] == "gobby#14972"
    catalogue = {entry["name"]: entry for entry in guarded["catalogue"]}
    anchor = seat_pane
    launched = []
    for step in definition.steps[1:]:
        if step.id == "arrange":
            continue  # tiles the tab after the seats; its own test covers it
        if not renderer.should_run_step(step, context):
            continue
        assert step.mcp is not None and step.mcp.arguments is not None
        args = renderer.render_mcp_arguments(step.mcp.arguments, context, drop_none=True)
        if step.id not in SEATS[pipeline]:
            _assert_support_step(step.id, step.mcp, args, seats, context)
            continue
        assert (step.mcp.server, step.mcp.tool) == ("gobby-agents", "spawn_agent")
        assert args["agent"] == step.id
        assert not {"provider", "model", "reasoning_effort"}.intersection(args)
        assert args["execution_mode"] == "interactive"
        assert args["checkout_mode"] == "none"
        assert args["project_path"] == "/pilot/shared-checkout"
        assert "worktree_id" not in args
        assert args["reserved_run_id"] == "pilot-run"
        assert ".gobby/roles" not in args["prompt"]
        assert ".gobby/plans/task-25186.md" in args["prompt"]
        assert "installed agent definition" in args["prompt"]
        assert args["prompt"].startswith(
            f"You are the {step.id.replace('-', ' ')} seat for task #25186 ("
        )
        assert 'target="session", target_id="gobby#14972"' in args["prompt"]
        assert "every SRT denial" in args["prompt"]
        assert "current session ref" in args["prompt"]
        assert 'owner_session="gobby#14972"' in args["prompt"]
        assert "wait_for_coordination" in args["prompt"]
        if anchor is None:
            assert args["placement"] == {
                "tab": {"workspace": "pilot-workspace", "title": "Team Planning - #25186"}
            }
            anchor = f"pilot:{step.id}"
        else:
            assert args["placement"] == {
                "split": {"pane": anchor, "axis": "right", "title": catalogue[step.id]["title"]}
            }
        context["steps"][step.id] = {
            "output": {"pane_ref": f"pilot:{step.id}", "child_session_id": f"{step.id}-session"}
        }
        launched.append(step.id)
    assert launched == list(seats)


@pytest.mark.parametrize(
    ("pipeline", "seat"), [(pipeline, seat) for pipeline, seats in SEATS.items() for seat in seats]
)
def test_team_runbook_preserves_operator_seat_overrides(pipeline: str, seat: str) -> None:
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
        }
    )
    inputs["task"] = "#25186"
    guard_output = {"tab_title": "Team Planning - #25186", "tab_exists": False, "launch": [seat]}
    context: dict[str, Any] = {
        "inputs": inputs,
        "steps": {
            "guard": {"output": guard_output},
            "target": {"output": {"title": "Planning runbook"}},
        },
        "invocation_id": "pilot-run",
    }
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
    inputs["seats"] = "unapproved-seat"
    assert not renderer.should_run_step(step, context)
