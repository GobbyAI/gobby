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
    return agent.prompt_for("agent") or ""


def test_planner_prompt_contains_grammar() -> None:
    prompt = _planner_prompt()
    assert "PLAN-COVERAGE CONTRACT TYPED GRAMMAR" in prompt
    assert "^#{2,6}" in prompt
    assert "deliverable | framing | verification | deferred" in prompt
    assert "**Acceptance:**" in prompt
    assert "task_ref" in prompt
    assert "original_acceptance_items" in prompt
    assert "table-row decomposition" in prompt.lower()


def test_planner_authors_narrative_only_not_the_manifest() -> None:
    """Planner authors narrative only; plan-adversary owns the manifest.

    Regression for the drift where planner.yaml instructed the planner to author
    and update `## M1 Task Manifest`, contradicting plan-draft and the
    plan-coverage contract (the adversary writes the manifest on approval).
    """
    prompt = _planner_prompt()
    assert "NARRATIVE ONLY" in prompt
    assert "plan-adversary` writes the manifest" in prompt
    # The planner must not be told to author/include/update the manifest itself.
    assert "include a `## M1 Task Manifest`" not in prompt
    assert "update the manifest in the" not in prompt


def test_planner_changelog_uses_v1_section_id() -> None:
    """The changelog heading must carry the `V1` section ID so the canonical
    heading regex recognizes it; a bare `## Plan Changelog` is dropped.
    """
    prompt = _planner_prompt()
    assert "## V1 Plan Changelog" in prompt
    assert "## Plan Changelog" not in prompt
