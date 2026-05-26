"""Wiring tests for planner.yaml — it must load the plan-draft methodology skill.

The stage-native planning flow spawns this agent to draft plans. Before
this wiring was added, planner.yaml had NO drafting methodology at all — only
"write a plan and mark it for review" instructions. That was a latent quality
gap; drafts produced under those instructions could violate the plan-draft
format in ways the expand pipeline would then silently drop.

These tests lock in:
  - there is a dedicated load_skill step between claim and plan,
  - that step only allows get_skill on gobby-skills,
  - the transition out of the step gates on a skill_loaded variable,
  - the inline instructions point explicitly at the plan-draft skill,
  - the escalation-prefix contract with the stage-native planning flow is
    preserved (needs_requirements: prefix).
"""

from pathlib import Path

import pytest
import yaml

from gobby.workflows.definitions import AgentDefinitionBody
from tests.agents._yaml_helpers import _field, find_step

pytestmark = pytest.mark.unit

PLANNER_PATH = Path("src/gobby/install/shared/workflows/agents/planner.yaml")


@pytest.fixture(scope="module")
def agent() -> AgentDefinitionBody:
    with PLANNER_PATH.open() as f:
        data = yaml.safe_load(f)
    return AgentDefinitionBody.model_validate(data)


class TestPlannerSkillLoading:
    def test_has_load_skill_step_between_claim_and_plan(self, agent: AgentDefinitionBody) -> None:
        """Ordering is load-bearing: skill must be in context before drafting."""
        names = [s.name for s in (agent.steps or [])]
        assert names == ["claim", "load_skill", "plan", "terminate"]

    def test_load_skill_step_targets_plan_draft(self, agent: AgentDefinitionBody) -> None:
        """Step status message must name the plan-draft skill explicitly so the
        runtime prompt contains the right get_skill(name=...) call."""
        load_step = find_step(agent.steps or [], "load_skill")
        assert load_step is not None
        assert load_step.status_message is not None
        assert "plan-draft" in load_step.status_message
        assert 'list_tools("gobby-skills")' in load_step.status_message
        assert 'get_tool_schema("gobby-skills", "get_skill")' in load_step.status_message
        assert (
            'call_tool("gobby-skills", "get_skill", {"name": "plan-draft"})'
            in load_step.status_message
        )
        assert "mcp__gobby__* proxy tools" in load_step.status_message
        assert "native Skill" in load_step.status_message
        assert "GitHub/app connector" in load_step.status_message
        assert "Computer Use tools" in load_step.status_message

    def test_load_skill_only_permits_get_skill(self, agent: AgentDefinitionBody) -> None:
        """Tight allow-list prevents the agent from wandering during skill
        load — no drafting edits, no unrelated MCP calls."""
        load_step = find_step(agent.steps or [], "load_skill")
        assert load_step is not None
        assert load_step.allowed_mcp_tools == ["gobby-skills:get_skill"]

    def test_load_skill_sets_skill_loaded_variable(self, agent: AgentDefinitionBody) -> None:
        """on_mcp_success contract: a successful get_skill flips skill_loaded,
        which is what the transition reads.

        on_mcp_success entries are dicts in the parsed YAML shape, not typed
        objects — use an isinstance-guarded extraction so this works for both
        dict-valued and (future) model-object entries.
        """
        load_step = find_step(agent.steps or [], "load_skill")
        assert load_step is not None
        mcp_success = getattr(load_step, "on_mcp_success", []) or []

        triples = [
            (_field(entry, "server"), _field(entry, "tool"), _field(entry, "variable"))
            for entry in mcp_success
        ]
        assert ("gobby-skills", "get_skill", "skill_loaded") in triples

    def test_transition_gates_on_skill_loaded(self, agent: AgentDefinitionBody) -> None:
        load_step = find_step(agent.steps or [], "load_skill")
        assert load_step is not None
        transitions = load_step.transitions or []
        assert any(t.to == "plan" and t.when and "skill_loaded" in t.when for t in transitions)


class TestPlannerInstructionsPreserveContracts:
    def test_instructions_reference_plan_draft(self, agent: AgentDefinitionBody) -> None:
        """Inline instructions must explicitly direct the agent to load
        plan-draft via get_skill — not generically 'follow the methodology'."""
        instructions = agent.instructions or ""
        assert "plan-draft" in instructions
        assert "get_skill" in instructions
        assert "native Skill" in instructions
        assert "GitHub/app connector" in instructions
        assert "Computer Use tools" in instructions
        assert "After `plan-draft` is loaded" in instructions

    def test_needs_requirements_escalation_preserved(self, agent: AgentDefinitionBody) -> None:
        """Contract with the stage-native planning flow: when context is
        insufficient, escalate with this exact prefix."""
        instructions = agent.instructions or ""
        assert "needs_requirements:" in instructions

    def test_critical_rules_preserved(self, agent: AgentDefinitionBody) -> None:
        """Worker-safety critical rules must survive the trim."""
        instructions = agent.instructions or ""
        for rule in (
            "close_task",
            "reopen_task",
            "approve_review",
            "reject_review",
            "spawn",
            "kill_agent",
            "uv run",
        ):
            assert rule in instructions, f"Missing critical rule: {rule}"
