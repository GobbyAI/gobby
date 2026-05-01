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
  - terminate-step exit wiring uses end_agent_run,
  - review rejection plus `needs_requirements:` survive the trim so the
    interactive planner's branching (Step 7.6) matches the autonomous state
    machine's.
"""

from pathlib import Path

import pytest
import yaml

from gobby.workflows.definitions import AgentDefinitionBody
from tests.agents._yaml_helpers import _field, find_step

pytestmark = pytest.mark.unit

ADVERSARY_PATH = Path("src/gobby/install/shared/workflows/agents/plan-adversary.yaml")


@pytest.fixture(scope="module")
def agent() -> AgentDefinitionBody:
    with ADVERSARY_PATH.open() as f:
        data = yaml.safe_load(f)
    return AgentDefinitionBody.model_validate(data)


class TestAdversarySkillLoading:
    def test_has_load_skill_step_between_claim_and_review(self, agent: AgentDefinitionBody) -> None:
        """Ordering is load-bearing: skill must be in context before reviewing."""
        names = [s.name for s in (agent.steps or [])]
        assert names == ["claim", "load_skill", "review", "terminate"]

    def test_load_skill_step_targets_plan_review(self, agent: AgentDefinitionBody) -> None:
        load_step = find_step(agent.steps or [], "load_skill")
        assert load_step is not None
        assert load_step.status_message is not None
        assert "plan-review" in load_step.status_message

    def test_load_skill_only_permits_get_skill(self, agent: AgentDefinitionBody) -> None:
        load_step = find_step(agent.steps or [], "load_skill")
        assert load_step is not None
        assert load_step.allowed_mcp_tools == ["gobby-skills:get_skill"]

    def test_load_skill_sets_skill_loaded_variable(self, agent: AgentDefinitionBody) -> None:
        # on_mcp_success entries are dicts in the parsed YAML shape, not typed
        # objects — keep the isinstance-guarded extraction so this works for
        # both dict-valued and (future) model-object entries.
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
        assert any(t.to == "review" and t.when and "skill_loaded" in t.when for t in transitions)

    def test_claim_step_uses_normal_delegated_claim(self, agent: AgentDefinitionBody) -> None:
        claim_step = find_step(agent.steps or [], "claim")
        assert claim_step is not None
        assert claim_step.status_message is not None
        assert "claim_task(task_id=assigned_task_id)" in claim_step.status_message
        assert "force=true" not in ADVERSARY_PATH.read_text()

    def test_claim_step_treats_closed_assigned_task_as_claim_complete(
        self, agent: AgentDefinitionBody
    ) -> None:
        claim_step = find_step(agent.steps or [], "claim")
        assert claim_step is not None
        mcp_error = getattr(claim_step, "on_mcp_error", []) or []
        handlers = [
            (_field(entry, "server"), _field(entry, "tool"), _field(entry, "variable"))
            for entry in mcp_error
        ]
        assert ("gobby-tasks", "claim_task", "task_claimed") in handlers
        claim_handlers = [
            entry
            for entry in mcp_error
            if _field(entry, "server") == "gobby-tasks" and _field(entry, "tool") == "claim_task"
        ]
        assert claim_handlers
        assert _field(claim_handlers[0], "when") == 'tool_output.error_code == "TASK_CLOSED"'


class TestAdversaryInstructionsPreserveContracts:
    def test_instructions_reference_plan_review(self, agent: AgentDefinitionBody) -> None:
        instructions = agent.instructions or ""
        assert "plan-review" in instructions
        assert "get_skill" in instructions

    def test_review_rejection_and_requirements_contracts_preserved(
        self, agent: AgentDefinitionBody
    ) -> None:
        """Revision rounds should use review rejection; insufficient-context
        halts should use `needs_requirements:`."""
        instructions = agent.instructions or ""
        assert "mark_task_review_rejected" in instructions
        assert "needs_requirements:" in instructions

    def test_round_scoped_findings_header_referenced(self, agent: AgentDefinitionBody) -> None:
        """Output goes under ## Adversary Findings — Round N; the inline
        instructions reinforce plan-review's format so a sloppy adversary
        run doesn't write a bare `## Adversary Findings` that leaks into
        the next round's view."""
        instructions = agent.instructions or ""
        assert "Adversary Findings" in instructions
        assert "Round N" in instructions or "display round" in instructions.lower()

    def test_review_step_completes_on_review_rejection(self, agent: AgentDefinitionBody) -> None:
        review_step = find_step(agent.steps or [], "review")
        assert review_step is not None
        mcp_success = getattr(review_step, "on_mcp_success", None) or []
        triples = [
            (_field(entry, "server"), _field(entry, "tool"), _field(entry, "variable"))
            for entry in mcp_success
        ]
        assert ("gobby-tasks", "mark_task_review_rejected", "review_complete") in triples

    def test_review_step_completes_on_closed_task_review_error(
        self, agent: AgentDefinitionBody
    ) -> None:
        review_step = find_step(agent.steps or [], "review")
        assert review_step is not None
        mcp_error = getattr(review_step, "on_mcp_error", None) or []
        triples = [
            (_field(entry, "server"), _field(entry, "tool"), _field(entry, "variable"))
            for entry in mcp_error
        ]
        assert ("gobby-tasks", "mark_task_review_approved", "review_complete") in triples
        assert ("gobby-tasks", "mark_task_review_rejected", "review_complete") in triples
        assert ("gobby-tasks", "escalate_task", "review_complete") in triples
        for tool in ("mark_task_review_approved", "mark_task_review_rejected", "escalate_task"):
            matches = [
                entry
                for entry in mcp_error
                if _field(entry, "server") == "gobby-tasks" and _field(entry, "tool") == tool
            ]
            assert matches
            assert _field(matches[0], "when") == 'tool_output.error_code == "TASK_CLOSED"'

    def test_critical_rules_preserved(self, agent: AgentDefinitionBody) -> None:
        """Worker-safety critical rules must survive the trim."""
        instructions = agent.instructions or ""
        for rule in (
            "close_task",
            "reopen_task",
            "mark_task_needs_review",
            "spawn",
            "end_agent_run",
            "uv run",
        ):
            assert rule in instructions, f"Missing critical rule: {rule}"

    def test_kill_agent_removed_from_instructions(self, agent: AgentDefinitionBody) -> None:
        instructions = agent.instructions or ""
        assert "kill_agent" not in instructions

    def test_call_tool_uses_ambient_session_context(self, agent: AgentDefinitionBody) -> None:
        instructions = agent.instructions or ""
        assert "Do NOT pass session_id to mcp__gobby__call_tool" in instructions


class TestAdversaryTerminateStep:
    def test_terminate_step_only_allows_end_agent_run(self, agent: AgentDefinitionBody) -> None:
        terminate = find_step(agent.steps or [], "terminate")
        assert terminate is not None
        assert terminate.allowed_mcp_tools == ["gobby-agents:end_agent_run"]

    def test_review_step_blocks_premature_end_agent_run(self, agent: AgentDefinitionBody) -> None:
        review = find_step(agent.steps or [], "review")
        assert review is not None
        blocked = review.blocked_mcp_tools or []
        assert "gobby-agents:end_agent_run" in blocked
