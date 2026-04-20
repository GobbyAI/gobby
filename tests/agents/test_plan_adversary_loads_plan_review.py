"""Wiring tests for plan-adversary.yaml — it must load the plan-review
methodology skill before reviewing.

Before this wiring was added, plan-adversary.yaml carried its own inline review
heuristics. Those heuristics were not available to the interactive planner's
adversarial loop, producing two different review policies for the same artifact
shape. Now both consumers load plan-review via get_skill, and this agent YAML
is trimmed to the escalation contract plus critical rules.

These tests lock in:
  - a dedicated load_skill step between claim and review,
  - only gobby-skills:get_skill permitted during that step,
  - transition out gates on skill_loaded,
  - instructions explicitly direct the agent to plan-review,
  - both escalation prefixes (planning_changes_requested:, needs_requirements:)
    survive the trim so the interactive planner's branching (Step 7.6) matches
    the autonomous state machine's.
"""

from pathlib import Path

import pytest
import yaml

from gobby.workflows.definitions import AgentDefinitionBody

pytestmark = pytest.mark.unit

ADVERSARY_PATH = Path("src/gobby/install/shared/workflows/agents/plan-adversary.yaml")


@pytest.fixture(scope="module")
def agent() -> AgentDefinitionBody:
    with ADVERSARY_PATH.open() as f:
        data = yaml.safe_load(f)
    return AgentDefinitionBody.model_validate(data)


class TestAdversarySkillLoading:
    def test_has_load_skill_step_between_claim_and_review(
        self, agent: AgentDefinitionBody
    ) -> None:
        """Ordering is load-bearing: skill must be in context before reviewing."""
        names = [s.name for s in (agent.steps or [])]
        assert names == ["claim", "load_skill", "review", "terminate"]

    def test_load_skill_step_targets_plan_review(self, agent: AgentDefinitionBody) -> None:
        load_step = next(s for s in (agent.steps or []) if s.name == "load_skill")
        assert load_step.status_message is not None
        assert "plan-review" in load_step.status_message

    def test_load_skill_only_permits_get_skill(self, agent: AgentDefinitionBody) -> None:
        load_step = next(s for s in (agent.steps or []) if s.name == "load_skill")
        assert load_step.allowed_mcp_tools == ["gobby-skills:get_skill"]

    def test_load_skill_sets_skill_loaded_variable(self, agent: AgentDefinitionBody) -> None:
        load_step = next(s for s in (agent.steps or []) if s.name == "load_skill")
        mcp_success = getattr(load_step, "on_mcp_success", None) or []
        triples = [
            (
                (entry.get("server") if isinstance(entry, dict) else getattr(entry, "server", None)),
                (entry.get("tool") if isinstance(entry, dict) else getattr(entry, "tool", None)),
                (
                    entry.get("variable")
                    if isinstance(entry, dict)
                    else getattr(entry, "variable", None)
                ),
            )
            for entry in mcp_success
        ]
        assert ("gobby-skills", "get_skill", "skill_loaded") in triples

    def test_transition_gates_on_skill_loaded(self, agent: AgentDefinitionBody) -> None:
        load_step = next(s for s in (agent.steps or []) if s.name == "load_skill")
        transitions = load_step.transitions or []
        assert any(
            t.to == "review" and t.when and "skill_loaded" in t.when for t in transitions
        )


class TestAdversaryInstructionsPreserveContracts:
    def test_instructions_reference_plan_review(self, agent: AgentDefinitionBody) -> None:
        instructions = agent.instructions or ""
        assert "plan-review" in instructions
        assert "get_skill" in instructions

    def test_both_escalation_prefixes_preserved(self, agent: AgentDefinitionBody) -> None:
        """The interactive planner Step 7.6 branches on these exact prefixes.
        Must match what's in plan-review and the _front_half.py state machine."""
        instructions = agent.instructions or ""
        assert "planning_changes_requested:" in instructions
        assert "needs_requirements:" in instructions

    def test_round_scoped_findings_header_referenced(
        self, agent: AgentDefinitionBody
    ) -> None:
        """Output goes under ## Adversary Findings — Round N; the inline
        instructions reinforce plan-review's format so a sloppy adversary
        run doesn't write a bare `## Adversary Findings` that leaks into
        the next round's view."""
        instructions = agent.instructions or ""
        assert "Adversary Findings" in instructions
        assert "Round N" in instructions or "display round" in instructions.lower()

    def test_critical_rules_preserved(self, agent: AgentDefinitionBody) -> None:
        """Worker-safety critical rules must survive the trim."""
        instructions = agent.instructions or ""
        for rule in (
            "close_task",
            "reopen_task",
            "mark_task_needs_review",
            "spawn",
            "kill_agent",
            "uv run",
        ):
            assert rule in instructions, f"Missing critical rule: {rule}"
