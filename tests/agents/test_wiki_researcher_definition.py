"""Contract tests for the bundled wiki-researcher agent definition.

wiki-researcher runs one research pass into the vault: claim the assigned
task, load the wiki-research methodology skill, research within hard budget
caps, close the task, terminate. These tests lock in:

  - the claim -> load_skill -> research -> terminate step machine,
  - only gobby-skills:get_skill permitted during load_skill,
  - the research step's MCP allowlist (gobby-wiki, gobby-tasks, get_skill,
    end_agent_run) and its lifecycle/spawn block list,
  - close_task and escalate_task both route to terminate,
  - max_turns stays out of the contract — the spawn path never enforced it
    (Codex #5), so the definition relies on `timeout` alone,
  - the spawn surface itself no longer accepts max_turns anywhere.
"""

import inspect
from pathlib import Path

import pytest
import yaml

from gobby.workflows.definitions import AgentDefinitionBody
from tests.agents._yaml_helpers import _field, find_step

pytestmark = pytest.mark.unit

AGENT_PATH = Path("src/gobby/install/shared/workflows/agents/wiki-researcher.yaml")


@pytest.fixture(scope="module")
def agent() -> AgentDefinitionBody:
    with AGENT_PATH.open() as f:
        data = yaml.safe_load(f)
    return AgentDefinitionBody.model_validate(data)


class TestWikiResearcherIdentity:
    def test_execution_profile(self, agent: AgentDefinitionBody) -> None:
        assert agent.name == "wiki-researcher"
        assert agent.surfaces == ["spawn"]
        assert agent.provider == "claude"
        assert agent.model == "sonnet"
        assert agent.isolation == "none"
        assert agent.timeout == 2700

    def test_max_turns_absent_from_contract(self, agent: AgentDefinitionBody) -> None:
        """Timeout is the only runtime limit; max_turns was never enforced."""
        with AGENT_PATH.open() as f:
            raw = yaml.safe_load(f)
        assert "max_turns" not in raw
        assert "max_turns" not in AgentDefinitionBody.model_fields

    def test_methodology_skill_declared(self, agent: AgentDefinitionBody) -> None:
        assert agent.skills.get("methodology") == ["wiki-research"]

    def test_agent_level_kill_agent_block(self, agent: AgentDefinitionBody) -> None:
        assert "gobby-agents:kill_agent" in agent.blocked_mcp_tools


class TestWikiResearcherStepMachine:
    def test_step_order(self, agent: AgentDefinitionBody) -> None:
        names = [s.name for s in (agent.steps or [])]
        assert names == ["claim", "load_skill", "research", "terminate"]

    def test_claim_step_only_permits_claim_and_get(self, agent: AgentDefinitionBody) -> None:
        claim = find_step(agent.steps or [], "claim")
        assert claim is not None
        assert claim.allowed_mcp_tools == ["gobby-tasks:claim_task", "gobby-tasks:get_task"]

    def test_load_skill_targets_wiki_research(self, agent: AgentDefinitionBody) -> None:
        load_step = find_step(agent.steps or [], "load_skill")
        assert load_step is not None
        assert load_step.allowed_mcp_tools == ["gobby-skills:get_skill"]
        assert load_step.status_message is not None
        assert (
            'call_tool("gobby-skills", "get_skill", {"name": "wiki-research"})'
            in load_step.status_message
        )

    def test_load_skill_sets_skill_loaded_variable(self, agent: AgentDefinitionBody) -> None:
        load_step = find_step(agent.steps or [], "load_skill")
        assert load_step is not None
        mcp_success = getattr(load_step, "on_mcp_success", []) or []
        triples = [
            (_field(entry, "server"), _field(entry, "tool"), _field(entry, "variable"))
            for entry in mcp_success
        ]
        assert ("gobby-skills", "get_skill", "skill_loaded") in triples

    def test_research_step_mcp_allowlist(self, agent: AgentDefinitionBody) -> None:
        research = find_step(agent.steps or [], "research")
        assert research is not None
        assert research.allowed_mcp_tools == [
            "gobby-wiki:*",
            "gobby-tasks:*",
            "gobby-skills:get_skill",
            "gobby-agents:end_agent_run",
            "gobby-sessions:record_verification_evidence",
        ]

    def test_research_step_blocks_lifecycle_and_spawn(self, agent: AgentDefinitionBody) -> None:
        research = find_step(agent.steps or [], "research")
        assert research is not None
        blocked = set(research.blocked_mcp_tools)
        assert {
            "gobby-tasks:reopen_task",
            "gobby-tasks:de_escalate_task",
            "gobby-tasks-ops:submit_for_review",
            "gobby-tasks-ops:approve_review",
            "gobby-tasks-ops:reject_review",
            "gobby-agents:spawn_agent",
            "gobby-agents:kill_agent",
        } <= blocked

    def test_close_and_escalate_both_route_to_terminate(self, agent: AgentDefinitionBody) -> None:
        research = find_step(agent.steps or [], "research")
        assert research is not None
        mcp_success = getattr(research, "on_mcp_success", []) or []
        triples = [
            (_field(entry, "server"), _field(entry, "tool"), _field(entry, "variable"))
            for entry in mcp_success
        ]
        assert ("gobby-tasks", "close_task", "task_closed") in triples
        assert ("gobby-tasks", "escalate_task", "task_escalated") in triples
        transitions = research.transitions or []
        assert any(
            t.to == "terminate"
            and t.when
            and "task_closed" in t.when
            and "task_escalated" in t.when
            for t in transitions
        )

    def test_lifecycle_triggers_scoped_to_assigned_task(self, agent: AgentDefinitionBody) -> None:
        """Closing or claiming an unrelated task must not advance the workflow.

        The first live run detoured into a daemon bugfix task; its close_task
        flipped the unscoped trigger and terminated the run before any
        research happened.
        """
        scope = "tool_input.get('task_id') == vars.get('assigned_task_id')"
        claim = find_step(agent.steps or [], "claim")
        research = find_step(agent.steps or [], "research")
        assert claim is not None and research is not None
        for step, tool in (
            (claim, "claim_task"),
            (research, "close_task"),
            (research, "escalate_task"),
        ):
            handlers = getattr(step, "on_mcp_success", []) or []
            matching = [h for h in handlers if _field(h, "tool") == tool]
            assert matching, f"no on_mcp_success handler for {tool}"
            assert all(_field(h, "when") == scope for h in matching), (
                f"{tool} trigger must be scoped to assigned_task_id"
            )

    def test_terminate_step_only_permits_end_agent_run(self, agent: AgentDefinitionBody) -> None:
        terminate = find_step(agent.steps or [], "terminate")
        assert terminate is not None
        assert terminate.allowed_mcp_tools == ["gobby-agents:end_agent_run"]
        assert agent.exit_condition == "current_step == 'terminate'"


class TestWikiResearcherInstructions:
    def test_critical_rules_present(self, agent: AgentDefinitionBody) -> None:
        instructions = agent.instructions or ""
        assert "git push" in instructions
        assert "Budgets are hard caps" in instructions
        assert "explicit `topic`" in instructions
        assert "Partial results are still results" in instructions
        assert "end_agent_run" in instructions

    def test_task_triage_present(self, agent: AgentDefinitionBody) -> None:
        instructions = agent.instructions or ""
        assert "task triage" in instructions

        research = next(step for step in agent.steps or [] if step.name == "research")
        assert "triage follow-up" in (research.status_message or "")


class TestSpawnSurfaceHasNoMaxTurns:
    """The spawn path never enforced max_turns; the parameter is gone."""

    def test_spawn_agent_impl_signature(self) -> None:
        from gobby.mcp_proxy.tools.spawn_agent._implementation import spawn_agent_impl

        assert "max_turns" not in inspect.signature(spawn_agent_impl).parameters

    def test_http_spawn_request_model(self) -> None:
        from gobby.servers.routes.agent_spawn import AgentSpawnRequest

        assert "max_turns" not in AgentSpawnRequest.model_fields
