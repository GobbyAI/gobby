"""The bundled team-planning runbook and its Plan Enhancer seat: plan validation and waits."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

pytestmark = pytest.mark.unit

INSTALL = Path(__file__).parents[2] / "src/gobby/install/shared/workflows"


def _yaml(relative: str) -> dict[str, Any]:
    loaded = yaml.safe_load((INSTALL / relative).read_text())
    assert isinstance(loaded, dict)
    return loaded


def test_writerless_planning_requires_its_registered_plan() -> None:
    pipeline = _yaml("pipelines/team-planning.yaml")
    step = next(s for s in pipeline["steps"] if s["id"] == "registered-plan")

    required = step["mcp"]["arguments"]["required"]

    assert isinstance(required, str), required
    assert "plan-writer" in required and "not in" in required


def test_omitted_seats_launch_every_planning_seat() -> None:
    pipeline = _yaml("pipelines/team-planning.yaml")
    guard = next(s for s in pipeline["steps"] if s["id"] == "guard")
    catalogue = [entry["name"] for entry in guard["mcp"]["arguments"]["catalogue"]]
    default = pipeline["inputs"]["seats"]["default"]

    assert sorted(default.split(",")) == sorted(catalogue), default


def test_enhancer_waits_on_every_live_planning_seat() -> None:
    prompt = str(_yaml("agents/plan-enhancer.yaml")["prompts"]["agent"])
    wait = prompt[prompt.index("For standing coordination") : prompt.index("Boundaries:")]

    assert "Plan Writer" in wait
    assert "Plan Adversary" in wait
    assert "report_to" in wait
