"""Planner prompt tests for the Plan-Coverage Contract typed grammar."""

import re
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
    """Planner authors narrative; review derives and the coordinator applies the manifest.

    Regression for the drift where planner.yaml instructed the planner to author
    and update `## M1 Task Manifest`, contradicting plan-draft and the
    plan-coverage contract (the adversary returns server-derived entries and the
    coordinator applies them after approval).
    """
    prompt = _planner_prompt()
    assert "NARRATIVE ONLY" in prompt
    assert "plan-adversary` returns server-derived manifest entries" in prompt
    assert "coordinator applies them through the review-evidence path" in prompt
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


def test_reference_contract_4_2_2() -> None:
    """Planning/build/review carriers preserve obligations and point to shipped topics."""
    shared = Path("src/gobby/install/shared")
    expected = {
        "planner": ("plan/drafting", "NARRATIVE ONLY"),
        "plan-adversary": ("plan/review", "review"),
        "plan-adversary-taskless": ("plan/review", "review"),
        "plan-enhancer": ("plan/enhancement", "You are advisory only"),
        "plan-enhancer-taskless": ("plan/enhancement", "You are advisory only"),
        "merge-orchestrator": ("build/coordination", "merge"),
        "epic-reviewer": ("review/epic", "Discovery Brief"),
        "backend-developer": ("development/obligations", "test-driven-development"),
        "default": ("skills/loading", "gobby-skills"),
    }
    for name, (topic, obligation) in expected.items():
        path = shared / "workflows" / "agents" / f"{name}.yaml"
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        agent = AgentDefinitionBody.model_validate(data)
        text = path.read_text(encoding="utf-8")
        assert f"references/{topic}.md" in text, name
        assert obligation in (agent.prompt_for("agent") or ""), name
        for reference in re.findall(r"references/[a-z-]+/[a-z-]+\.md", text):
            assert (shared / "skills" / "gobby" / reference).is_file(), (name, reference)
    for filename in ("system.md", "user.md"):
        prompt = (shared / "prompts" / "expansion" / filename).read_text(encoding="utf-8")
        assert "gobby:references/plan/expansion.md" in prompt
        assert "additional_skills" in prompt
        assert "get_skill_file" in prompt and "get_tool_schema" in prompt
        assert "page.next_cursor" in prompt
        assert "assigned_agent" in prompt
        assert "expansion-agent-selection" not in prompt
