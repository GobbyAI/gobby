"""The research runbook launches a Researcher and a Research Reviewer into one tab in the main checkout."""

from __future__ import annotations

from itertools import combinations
from pathlib import Path
from typing import Any

import pytest
import yaml

from gobby.workflows.definitions import PipelineDefinition
from gobby.workflows.pipeline.renderer import StepRenderer
from gobby.workflows.templates import TemplateEngine

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
RESEARCH_PATH = ROOT / "src/gobby/install/shared/workflows/pipelines/research.yaml"
SEATS = ("researcher", "research-reviewer")
SEAT_TITLES = {"researcher": "Researcher", "research-reviewer": "Research Reviewer"}
SHARED_INPUTS = {"workspace", "task", "seat_pane", "project_path", "seats", "report_to"}
SEAT_INPUTS = {
    f"{seat.replace('-', '_')}_{name}"
    for seat in SEATS
    for name in ("provider", "model", "reasoning_effort")
}
TAB_TITLE = "Research - #25223"


def _definition() -> PipelineDefinition:
    assert RESEARCH_PATH.exists(), "the research runbook is not bundled"
    return PipelineDefinition.model_validate(yaml.safe_load(RESEARCH_PATH.read_text()))


def _context(
    definition: PipelineDefinition,
    launch: tuple[str, ...],
    seat_pane: str | None = None,
    **inputs: Any,
) -> dict[str, Any]:
    values = {name: spec.get("default") for name, spec in definition.inputs.items()}
    values.update(
        seats=",".join(SEATS),
        task="#25223",
        workspace="pilot-workspace",
        project_path="/pilot/shared-checkout",
        seat_pane=seat_pane,
        report_to="gobby#14972",
    )
    values.update(inputs)
    guard_output = {"tab_title": TAB_TITLE, "launch": list(launch)}
    return {
        "inputs": values,
        "steps": {"guard": {"output": guard_output}},
        "invocation_id": "pilot-run",
    }


def test_research_runbook_is_a_tagged_resumable_runbook() -> None:
    definition = _definition()

    assert definition.name == "research"
    assert definition.tags == ["runbook"]
    assert definition.resume_on_restart
    assert [step.id for step in definition.steps] == ["guard", "admit", *SEATS, "arrange"]


def test_research_inputs_name_the_shared_and_per_seat_settings() -> None:
    definition = _definition()

    assert set(definition.inputs) == SHARED_INPUTS | SEAT_INPUTS
    assert definition.inputs["seats"]["default"] == ",".join(SEATS)
    for name in ("workspace", "task", "project_path", "report_to"):
        assert definition.inputs[name]["required"], name
    assert definition.inputs["seat_pane"]["default"] is None


def test_research_guard_launches_only_the_seats_not_already_live() -> None:
    guard = _definition().steps[0].mcp
    assert guard is not None and guard.arguments is not None

    assert (guard.server, guard.tool) == ("gobby-agents", "check_runbook_seats")
    assert guard.arguments["require_report_to"] is True
    assert guard.arguments["skip_live_seats"] is True
    assert guard.arguments["tab_title"] == "Research"
    assert guard.arguments["task"] == "${{ inputs.task }}"
    assert [(entry["name"], entry["agent"]) for entry in guard.arguments["catalogue"]] == [
        (seat, seat) for seat in SEATS
    ]


@pytest.mark.parametrize("launch", [(), ("researcher",), ("research-reviewer",), SEATS])
def test_research_seats_run_exactly_when_the_guard_launches_them(launch: tuple[str, ...]) -> None:
    definition = _definition()
    context = _context(definition, launch)
    renderer = StepRenderer(TemplateEngine())

    ran = [
        step.id
        for step in definition.steps
        if step.id in SEATS and renderer.should_run_step(step, context)
    ]

    assert ran == [seat for seat in SEATS if seat in launch]


@pytest.mark.parametrize("seat_pane", [None, "pilot:existing-pane"])
@pytest.mark.parametrize(
    "launch",
    [subset for size in (1, 2) for subset in combinations(SEATS, size)],
)
def test_research_seats_join_one_tab_in_the_main_checkout(
    launch: tuple[str, ...], seat_pane: str | None
) -> None:
    definition = _definition()
    context = _context(definition, launch, seat_pane=seat_pane)
    renderer = StepRenderer(TemplateEngine())
    anchor = seat_pane
    launched = []
    for step in definition.steps[1:]:
        if step.id in ("admit", "arrange") or not renderer.should_run_step(step, context):
            continue
        assert step.mcp is not None and step.mcp.arguments is not None
        assert (step.mcp.server, step.mcp.tool) == ("gobby-agents", "spawn_agent")
        args = renderer.render_mcp_arguments(step.mcp.arguments, context, drop_none=True)

        assert args["agent"] == step.id
        assert args["execution_mode"] == "interactive"
        assert args["checkout_mode"] == "none"
        assert args["project_path"] == "/pilot/shared-checkout"
        assert "worktree_id" not in args
        assert args["reserved_run_id"] == "pilot-run"
        assert args["prompt"].startswith(f"You are the {step.id.replace('-', ' ')} seat.")
        assert 'target="session", target_id="gobby#14972"' in args["prompt"]
        assert "every SRT denial" in args["prompt"]
        assert "installed agent definition" in args["prompt"]
        assert 'owner_session="gobby#14972"' in args["prompt"]
        assert "wait_for_coordination" in args["prompt"]
        if anchor is None:
            assert args["placement"] == {
                "tab": {"workspace": "pilot-workspace", "title": TAB_TITLE}
            }
            anchor = f"pilot:{step.id}"
        else:
            assert args["placement"] == {
                "split": {"pane": anchor, "axis": "right", "title": SEAT_TITLES[step.id]}
            }
        context["steps"][step.id] = {"output": {"pane_ref": f"pilot:{step.id}"}}
        launched.append(step.id)

    assert launched == [seat for seat in SEATS if seat in launch]


def test_research_seats_carry_their_own_provider_model_and_effort() -> None:
    definition = _definition()
    context = _context(
        definition,
        SEATS,
        researcher_provider="provider-a",
        researcher_model="model-a",
        researcher_reasoning_effort="effort-a",
        research_reviewer_provider="provider-b",
        research_reviewer_model="model-b",
        research_reviewer_reasoning_effort="effort-b",
    )
    renderer = StepRenderer(TemplateEngine())

    settings = {}
    for step in definition.steps:
        if step.id in SEATS:
            assert step.mcp is not None and step.mcp.arguments is not None
            args = renderer.render_mcp_arguments(step.mcp.arguments, context, drop_none=True)
            settings[step.id] = (args["provider"], args["model"], args["reasoning_effort"])

    assert settings == {
        "researcher": ("provider-a", "model-a", "effort-a"),
        "research-reviewer": ("provider-b", "model-b", "effort-b"),
    }


def test_research_arrange_tiles_the_tab_its_seats_joined() -> None:
    definition = _definition()
    arrange = definition.steps[-1]
    assert arrange.id == "arrange"
    assert arrange.condition == "${{ len(steps.guard.output.launch) > 0 }}"
    assert arrange.mcp is not None and arrange.mcp.arguments is not None
    assert (arrange.mcp.server, arrange.mcp.tool) == ("gobby-workspaces", "rebalance_tab")
    raw_arrange = yaml.safe_load(RESEARCH_PATH.read_text())["steps"][-1]
    assert raw_arrange["mcp"]["on_refused"] == "record"

    context = _context(definition, SEATS)
    context["steps"]["researcher"] = {
        "output": {"pane_ref": "pilot:researcher", "tab_ref": "tab-7"}
    }
    context["steps"]["research-reviewer"] = {"output": {"pane_ref": "pilot:reviewer"}}
    args = StepRenderer(TemplateEngine()).render_mcp_arguments(
        arrange.mcp.arguments, context, drop_none=True
    )

    assert args["tab"] == "tab-7"
    assert args["layout"] == "tiled"
    assert args["seats"] == [
        {"name": "researcher", "pane": "pilot:researcher"},
        {"name": "research-reviewer", "pane": "pilot:reviewer"},
    ]


def test_research_admits_a_treeless_team_when_a_seat_launches() -> None:
    definition = _definition()
    admit = next(step for step in definition.steps if step.id == "admit")

    assert admit.condition == "${{ len(steps.guard.output.launch) > 0 }}"
    assert admit.mcp is not None and admit.mcp.arguments is not None
    assert (admit.mcp.server, admit.mcp.tool) == ("gobby-agents", "admit_team")
    args = StepRenderer(TemplateEngine()).render_mcp_arguments(
        admit.mcp.arguments, _context(definition, SEATS), drop_none=True
    )
    assert args == {
        "task": "#25223",
        "checkout_mode": "none",
        "tab_title": "Research - #25223",
    }


def test_research_seats_route_their_run_without_a_dev_manager() -> None:
    definition = _definition()
    context = _context(definition, SEATS)
    renderer = StepRenderer(TemplateEngine())
    prompts = {}
    for step in definition.steps:
        if step.id in SEATS:
            assert step.mcp is not None and step.mcp.arguments is not None
            args = renderer.render_mcp_arguments(step.mcp.arguments, context, drop_none=True)
            prompts[step.id] = args["prompt"]

    researcher, reviewer = prompts["researcher"], prompts["research-reviewer"]
    assert "EVENT=CANDIDATE TASK=#25223" in researcher
    assert "EVENT=READY_TO_CLOSE TASK=#25223" in researcher
    assert "Do not launch a DevTeam" in researcher
    assert "no Dev Manager" in reviewer
    assert "LAND" in reviewer and "BOUNCE" in reviewer
    assert "EVENT=READY_TO_CLOSE" not in reviewer
