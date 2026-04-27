"""Planner prompt tests for the Plan-Coverage Contract typed grammar."""

from pathlib import Path

import pytest
import yaml

from gobby.workflows.definitions import AgentDefinitionBody

pytestmark = pytest.mark.unit

PLANNER = Path("src/gobby/install/shared/workflows/agents/planner.yaml")


def _planner_prompt() -> str:
    data = yaml.safe_load(PLANNER.read_text(encoding="utf-8"))
    agent = AgentDefinitionBody.model_validate(data)
    return agent.build_prompt_preamble() or ""


def test_planner_prompt_contains_grammar() -> None:
    prompt = _planner_prompt()
    assert "PLAN-COVERAGE CONTRACT TYPED GRAMMAR" in prompt
    assert "^#{2,6}" in prompt
    assert "deliverable | framing | verification | deferred" in prompt
    assert "**Acceptance:**" in prompt
    assert "task_ref" in prompt
    assert "original_acceptance_items" in prompt
    assert "table-row decomposition" in prompt.lower()
