"""Validate Josh's project crew configuration without launching live seats."""

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

from gobby.workflows.pipeline.renderer import StepRenderer
from gobby.workflows.pipeline_models import PipelineDefinition

ROOT = Path(__file__).resolve().parents[2]
PIPELINES = ROOT / ".gobby/workflows/pipelines"


def _definitions() -> list[PipelineDefinition]:
    paths = sorted(PIPELINES.glob("crew-*.yaml"))
    assert paths, "Crew relaunch pipelines must exist in project configuration"
    return [PipelineDefinition.model_validate(yaml.safe_load(path.read_text())) for path in paths]


def test_crew_catalogue_covers_current_roster_once() -> None:
    # Multiple seats can share one role file.
    expected = {
        ref: role
        for role, ref in re.findall(
            r"\| ([\w-]+\.md) \| gobby#(\d+) \|", (ROOT / ".gobby/roles/roster.md").read_text()
        )
    }
    actual: dict[str, str] = {}
    for definition in _definitions():
        assert "runbook" in definition.tags
        assert definition.resume_on_restart
        guard = definition.steps[0].mcp
        assert guard is not None and guard.tool == "check_runbook_seats"
        assert guard.arguments is not None
        for seat in guard.arguments["catalogue"]:
            ref = seat["name"].removeprefix("seat_")
            assert ref not in actual, f"Duplicate roster seat {ref}"
            step = definition.get_step(seat["name"])
            assert step is not None and step.mcp is not None
            assert step.mcp.arguments is not None
            actual[ref] = next(
                role
                for role in expected.values()
                if f".gobby/roles/{role}" in step.mcp.arguments["prompt"]
            )
    assert actual == expected


@pytest.mark.parametrize("selection", ["all", "single", "alternating", "overrides"])
def test_crew_partial_relaunch_renders_valid_placement(selection: str) -> None:
    renderer = StepRenderer(None)
    for definition in _definitions():
        names = definition.inputs["seats"]["default"].split(",")
        selections = (
            [[name] for name in names]
            if selection == "single"
            else [names[::2] if selection == "alternating" else names]
        )
        for requested in selections:
            inputs = {
                name: spec.get("default", "pilot-value") for name, spec in definition.inputs.items()
            }
            inputs["seats"] = ",".join(requested)
            if selection == "overrides":
                inputs["codex_model"] = "gpt-6.1-sol"
                inputs["codex_effort"] = "medium"
            context: dict[str, Any] = {"inputs": inputs, "steps": {}, "invocation_id": "pilot-run"}
            launched: list[str] = []
            for step in definition.steps[1:]:
                if not renderer.should_run_step(step, context):
                    continue
                assert step.mcp is not None and step.mcp.tool == "spawn_agent"
                assert step.mcp.arguments is not None
                args = renderer.render_mcp_arguments(step.mcp.arguments, context, drop_none=True)
                assert args["reserved_run_id"] == "pilot-run"
                assert args["execution_mode"] == "interactive"
                assert args["agent"] in {"developer", "default"}
                assert args["provider"] in {"claude", "codex"}
                if (
                    step.id
                    in {"seat_15406", "seat_15401", "seat_15414", "seat_15470", "seat_15471"}
                    and selection != "overrides"
                ):
                    assert "model" not in args and "reasoning_effort" not in args
                else:
                    assert args["model"] and args["reasoning_effort"]
                if args["provider"] == "codex" and step.id not in {
                    "seat_15406",
                    "seat_15401",
                    "seat_15414",
                    "seat_15470",
                    "seat_15471",
                }:
                    assert (args["model"], args["reasoning_effort"]) == ("gpt-6.1-sol", "medium")
                assert args["checkout_mode"] == "none"
                assert bool(args.get("worktree_id")) != bool(args.get("project_path"))
                if launched:
                    assert args["placement"]["split"]["pane"] == f"pane-{launched[-1]}"
                    assert args["placement"]["split"]["axis"] == (
                        "right" if names.index(step.id) % 2 else "down"
                    )
                else:
                    assert args["placement"]["tab"]["workspace"] == inputs["workspace"]
                launched.append(step.id)
                context["steps"][step.id] = {"output": {"pane_ref": f"pane-{step.id}"}}
            assert launched == requested
