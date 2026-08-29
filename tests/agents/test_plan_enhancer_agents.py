"""Structural contract tests for the bundled plan-enhancer agents.

`plan-enhancer-taskless` (interactive /gobby plan) and `plan-enhancer`
(stage-native dispatch) are the constructive Better/Bigger counterweight to the
plan-adversary agents. They run BEFORE the unchanged adversary gate and are
advisory-only: they load `plan-enhance` + `proportionality`, never edit the plan
(Edit/Write blocked), and never approve, reject, or write the manifest. The
taskless variant emits suggestions to the parent via `send_message` then
`end_agent_run`; the stage-native variant records them through
`record_plan_enhancement` (never an approve/reject verb).

These tests guard that contract against drift in the YAML definitions and prove
both files parse through the same `AgentDefinitionBody` validation that
`sync_bundled_agents` applies.
"""

from pathlib import Path

import pytest
import yaml

from gobby.workflows.definitions import AgentDefinitionBody
from tests.agents._yaml_helpers import _field, find_step

pytestmark = pytest.mark.unit

AGENTS_DIR = Path("src/gobby/install/shared/workflows/agents")
TASKLESS_PATH = AGENTS_DIR / "plan-enhancer-taskless.yaml"
STAGE_NATIVE_PATH = AGENTS_DIR / "plan-enhancer.yaml"


def _load(path: Path) -> AgentDefinitionBody:
    with path.open() as f:
        data = yaml.safe_load(f)
    return AgentDefinitionBody.model_validate(data)


@pytest.fixture
def taskless() -> AgentDefinitionBody:
    return _load(TASKLESS_PATH)


@pytest.fixture
def stage_native() -> AgentDefinitionBody:
    return _load(STAGE_NATIVE_PATH)


class TestSharedEnhancerContract:
    """Invariants that hold for both enhancer agents."""

    @pytest.mark.parametrize("path", [TASKLESS_PATH, STAGE_NATIVE_PATH])
    def test_parses_through_agent_definition_body(self, path: Path) -> None:
        # This is exactly what sync_bundled_agents does before persisting.
        agent = _load(path)
        assert agent.name == path.stem

    @pytest.mark.parametrize("fixture", ["taskless", "stage_native"])
    def test_uses_codex_gpt56_sol_xhigh(self, fixture: str, request: pytest.FixtureRequest) -> None:
        agent: AgentDefinitionBody = request.getfixturevalue(fixture)
        assert agent.provider == "codex"
        assert agent.model == "gpt-5.6-sol"
        assert agent.reasoning_effort == "xhigh"

    @pytest.mark.parametrize("fixture", ["taskless", "stage_native"])
    def test_blocks_edit_and_write_at_agent_level(
        self, fixture: str, request: pytest.FixtureRequest
    ) -> None:
        agent: AgentDefinitionBody = request.getfixturevalue(fixture)
        blocked = agent.blocked_tools or []
        assert "Edit" in blocked
        assert "Write" in blocked

    @pytest.mark.parametrize("fixture", ["taskless", "stage_native"])
    def test_load_skill_targets_plan_enhance_and_proportionality(
        self, fixture: str, request: pytest.FixtureRequest
    ) -> None:
        agent: AgentDefinitionBody = request.getfixturevalue(fixture)
        load_step = find_step(
            (agent.step_workflow.steps if agent.step_workflow else []), "load_skill"
        )
        assert load_step is not None
        assert load_step.status_message is not None
        assert "plan-enhance" in load_step.status_message
        assert "proportionality" in load_step.status_message
        mcp_success = getattr(load_step, "on_mcp_success", []) or []
        triples = [
            (_field(e, "server"), _field(e, "tool"), _field(e, "variable")) for e in mcp_success
        ]
        assert ("gobby-skills", "get_skill", "skill_loaded") in triples
        assert ("gobby-skills", "get_skill", "proportionality_loaded") in triples
        assert ("gobby-skills", "get_skill", "restraint_loaded") in triples

    @pytest.mark.parametrize("fixture", ["taskless", "stage_native"])
    def test_load_skill_transition_gates_on_all_three_skills(
        self, fixture: str, request: pytest.FixtureRequest
    ) -> None:
        agent: AgentDefinitionBody = request.getfixturevalue(fixture)
        load_step = find_step(
            (agent.step_workflow.steps if agent.step_workflow else []), "load_skill"
        )
        assert load_step is not None
        transitions = getattr(load_step, "transitions", []) or []
        gate = [_field(t, "when") for t in transitions if _field(t, "to") == "enhance"]
        assert gate
        assert "vars.skill_loaded and vars.proportionality_loaded and vars.restraint_loaded" in gate

    @pytest.mark.parametrize("fixture", ["taskless", "stage_native"])
    def test_instructions_reference_methodology_skills(
        self, fixture: str, request: pytest.FixtureRequest
    ) -> None:
        agent: AgentDefinitionBody = request.getfixturevalue(fixture)
        assert agent.prompts.agent is not None
        assert "plan-enhance" in agent.prompts.agent
        assert "proportionality" in agent.prompts.agent

    @pytest.mark.parametrize("fixture", ["taskless", "stage_native"])
    def test_never_edits_plan_or_writes_manifest_in_instructions(
        self, fixture: str, request: pytest.FixtureRequest
    ) -> None:
        agent: AgentDefinitionBody = request.getfixturevalue(fixture)
        assert agent.prompts.agent is not None
        lowered = agent.prompts.agent.lower()
        assert "manifest" in lowered  # explicitly forbidden
        assert "edit" in lowered and "write" in lowered


class TestTasklessEnhancer:
    """plan-enhancer-taskless: advisory, surfaces suggestions to the parent."""

    def test_isolation_none(self, taskless: AgentDefinitionBody) -> None:
        assert taskless.isolation == "none"

    def test_has_no_claim_step(self, taskless: AgentDefinitionBody) -> None:
        # Taskless reviewers never claim or mutate Gobby tasks.
        assert (
            find_step((taskless.step_workflow.steps if taskless.step_workflow else []), "claim")
            is None
        )

    def test_enhance_step_emits_via_send_message_and_end_run(
        self, taskless: AgentDefinitionBody
    ) -> None:
        enhance = find_step(
            (taskless.step_workflow.steps if taskless.step_workflow else []), "enhance"
        )
        assert enhance is not None
        allowed = enhance.allowed_mcp_tools or []
        assert "gobby-agents:send_message" in allowed
        assert "gobby-agents:end_agent_run" in allowed

    def test_enhance_completes_on_end_agent_run(self, taskless: AgentDefinitionBody) -> None:
        enhance = find_step(
            (taskless.step_workflow.steps if taskless.step_workflow else []), "enhance"
        )
        assert enhance is not None
        mcp_success = getattr(enhance, "on_mcp_success", []) or []
        triples = [
            (_field(e, "server"), _field(e, "tool"), _field(e, "variable")) for e in mcp_success
        ]
        assert ("gobby-agents", "end_agent_run", "enhance_complete") in triples

    def test_enhance_completes_on_result_delivery(self, taskless: AgentDefinitionBody) -> None:
        # end_agent_run takes no arguments, so the result must be sent first. If
        # the provider process exits after that send, no turn_end fires and the
        # require-step-completion stop gate cannot ask for end_agent_run, so a
        # delivered round would be recorded as a failed run. Delivering the
        # result latches completion; the message_type gate stops mid-run
        # messages from latching it early.
        enhance = find_step(
            (taskless.step_workflow.steps if taskless.step_workflow else []), "enhance"
        )
        assert enhance is not None
        mcp_success = getattr(enhance, "on_mcp_success", []) or []
        latches = [
            e
            for e in mcp_success
            if (_field(e, "server"), _field(e, "tool")) == ("gobby-agents", "send_message")
            and _field(e, "variable") == "enhance_complete"
        ]
        assert len(latches) == 1
        assert _field(latches[0], "when") == "tool_input.message_type == 'plan_enhancement'"
        assert "plan_enhancement" in (taskless.prompts.agent or "")

    def test_instructions_route_artifact_read_to_the_filesystem(
        self, taskless: AgentDefinitionBody
    ) -> None:
        # The enhance step whitelists a handful of MCP tools and none of them
        # read files, so a prompt that only says "read the plan artifact" leads
        # the agent to conclude the artifact is unreachable and stall the parent
        # for a paste. isolation=none puts artifact_path on the local disk; both
        # the agent prompt and the step message must say to read it there.
        enhance = find_step(
            (taskless.step_workflow.steps if taskless.step_workflow else []), "enhance"
        )
        assert enhance is not None
        assert enhance.status_message is not None
        assert taskless.prompts.agent is not None
        for text in (taskless.prompts.agent, enhance.status_message):
            # Block scalars hard-wrap, so compare against unwrapped text.
            unwrapped = " ".join(text.lower().split())
            assert "artifact_path" in unwrapped
            assert "local filesystem" in unwrapped
            assert "native file-read or shell" in unwrapped

    def test_enhance_step_blocks_review_and_record_verbs(
        self, taskless: AgentDefinitionBody
    ) -> None:
        enhance = find_step(
            (taskless.step_workflow.steps if taskless.step_workflow else []), "enhance"
        )
        assert enhance is not None
        blocked = enhance.blocked_mcp_tools or []
        for tool in (
            "gobby-tasks-ops:approve_review",
            "gobby-tasks-ops:reject_review",
            "gobby-tasks-ops:record_plan_enhancement",
            "gobby-tasks:claim_task",
        ):
            assert tool in blocked, f"{tool} should be blocked for taskless enhancer"

    def test_exit_condition_on_enhance_complete(self, taskless: AgentDefinitionBody) -> None:
        assert taskless.step_workflow is not None
        assert taskless.step_workflow.exit_condition == "vars.enhance_complete"


class TestStageNativeEnhancer:
    """plan-enhancer: records suggestions via record_plan_enhancement."""

    def test_has_claim_step_with_delegated_close_tolerance(
        self, stage_native: AgentDefinitionBody
    ) -> None:
        claim = find_step(
            (stage_native.step_workflow.steps if stage_native.step_workflow else []), "claim"
        )
        assert claim is not None
        mcp_error = getattr(claim, "on_mcp_error", []) or []
        closed = [
            _field(e, "when")
            for e in mcp_error
            if _field(e, "server") == "gobby-tasks" and _field(e, "tool") == "claim_task"
        ]
        assert any("TASK_CLOSED" in (w or "") for w in closed)

    def test_enhance_completes_on_record_plan_enhancement(
        self, stage_native: AgentDefinitionBody
    ) -> None:
        enhance = find_step(
            (stage_native.step_workflow.steps if stage_native.step_workflow else []), "enhance"
        )
        assert enhance is not None
        mcp_success = getattr(enhance, "on_mcp_success", []) or []
        triples = [
            (_field(e, "server"), _field(e, "tool"), _field(e, "variable")) for e in mcp_success
        ]
        assert (
            "gobby-tasks-ops",
            "record_plan_enhancement",
            "enhance_complete",
        ) in triples

    def test_enhance_step_blocks_gate_verbs_and_premature_end(
        self, stage_native: AgentDefinitionBody
    ) -> None:
        enhance = find_step(
            (stage_native.step_workflow.steps if stage_native.step_workflow else []), "enhance"
        )
        assert enhance is not None
        blocked = enhance.blocked_mcp_tools or []
        for tool in (
            "gobby-tasks-ops:approve_review",
            "gobby-tasks-ops:reject_review",
            "gobby-agents:end_agent_run",
        ):
            assert tool in blocked, f"{tool} should be blocked in the enhance step"

    def test_instructions_record_plan_enhancement_not_gate(
        self, stage_native: AgentDefinitionBody
    ) -> None:
        assert stage_native.prompts.agent is not None
        assert "record_plan_enhancement" in stage_native.prompts.agent
        lowered = stage_native.prompts.agent.lower()
        assert "approve_review" in stage_native.prompts.agent
        assert "reject_review" in stage_native.prompts.agent
        # Framed as prohibitions ("never gate").
        assert "never" in lowered

    def test_terminate_step_only_allows_end_agent_run(
        self, stage_native: AgentDefinitionBody
    ) -> None:
        terminate = find_step(
            (stage_native.step_workflow.steps if stage_native.step_workflow else []), "terminate"
        )
        assert terminate is not None
        assert terminate.allowed_mcp_tools == ["gobby-agents:end_agent_run"]

    def test_exit_condition_on_terminate_step(self, stage_native: AgentDefinitionBody) -> None:
        assert stage_native.step_workflow is not None
        assert stage_native.step_workflow.exit_condition == "current_step == 'terminate'"
