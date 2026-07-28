"""Tests for ordered stage-native dispatcher decision rules."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.unit


_STAGE_AGENTS = {
    "ideation": "analyst",
    "research": "researcher",
    "architecture": "architect",
    "prd": "product-manager",
    "planning": "planner",
    "development": "backend-developer",
    "epic_qa": "epic-reviewer",
    "pr": "merge-orchestrator",
    "merge": "merge-orchestrator",
}

_REVIEW_AGENTS = {
    "planning": "plan-adversary",
    "expansion": "expansion-qa",
    "development": "qa-reviewer",
}


def _stage(stage_name: str, state: str, position: int = 0, **overrides):
    values = {
        "name": stage_name,
        "stage_name": stage_name,
        "state": state,
        "position": position,
        "work_attempt_count": 0,
        "review_round_count": 0,
        "max_work_attempts": None,
        "max_review_rounds": None,
        "requires_human": False,
        "review_policy": "required",
        "reviewer_agent": _REVIEW_AGENTS.get(stage_name),
        "default_agent": _STAGE_AGENTS.get(stage_name),
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _registry(stage_name: str, **overrides):
    values = {
        "name": stage_name,
        "default_agent": _STAGE_AGENTS.get(stage_name),
        "requires_human": False,
        "default_max_work_attempts": 3,
        "default_max_review_rounds": 2,
        "reviewer_agent": _REVIEW_AGENTS.get(stage_name),
        "dispatch_type": None,
        "dispatch_target": None,
        "dispatch_inputs_json": None,
    }
    if stage_name == "expansion":
        values.update(
            {
                "dispatch_type": "pipeline",
                "dispatch_target": "02e3e743-e572-51b3-a0f4-83e68271282f",
                "dispatch_inputs_json": '{"task_id": "${{ task_id }}"}',
            }
        )
    values.update(overrides)
    return SimpleNamespace(**values)


def _agents(**overrides):
    agents = {
        agent_slug: SimpleNamespace(name=agent_slug, enabled=True)
        for agent_slug in {
            *_STAGE_AGENTS.values(),
            *_REVIEW_AGENTS.values(),
            "doc-reviewer",
            "frontend-developer",
            "fullstack-developer",
        }
    }
    agents.update(overrides)
    return agents


def _task(**overrides):
    values = {
        "id": "7d34e462-6ba3-5a6c-b1c6-1584b855cb83",
        "ref": "#1",
        "task_type": "task",
        "labels": [],
        "is_closed": False,
        "is_escalated": False,
        "allow_automation": True,
        "unattended": False,
        "isolation": "none",
        "assigned_agent": "backend-developer",
        "blocked_by": set(),
        "active_blocked_by": set(),
        "stages": [_stage("development", "in_progress")],
        "children": [],
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _task_at(stage_name: str, state: str, **overrides):
    stage_overrides = overrides.pop("stage_overrides", {})
    values = {"stages": [_stage(stage_name, state, **stage_overrides)]}
    values.update(overrides)
    return _task(**values)


def _artifacts(**overrides):
    values = {
        "plan_file_path": None,
        "plan_file_hash": None,
        "last_reviewed_plan_hash": None,
        "worktree_path": None,
        "worktree_id": None,
        "clone_path": None,
        "clone_id": None,
        "integration_branch": None,
        "integration_workspace_id": None,
        "integration_clone_id": None,
        "base_commit_sha": None,
        "target_branch": "main",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _context(**overrides):
    values = {
        "artifacts": _artifacts(),
        "children": [],
        "stage_registry": {
            stage_name: _registry(stage_name)
            for stage_name in {
                *_STAGE_AGENTS,
                *_REVIEW_AGENTS,
                "expansion",
            }
        },
        "agents": _agents(),
        "prompt_context": {},
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _evaluate(task, context=None):
    from gobby.dispatch.rules import evaluate

    return evaluate(task, context or _context())


def test_planning_review_rule_fires_on_needs_review_stage() -> None:
    from gobby.dispatch.actions import SpawnAgentAction

    action = _evaluate(_task_at("planning", "needs_review"))

    assert isinstance(action, SpawnAgentAction)
    assert action.agent_slug == "plan-adversary"
    assert action.initial_variables == {
        "stage_name": "planning",
        "stage_state": "needs_review",
    }


def test_spawn_agent_action_uses_seq_ref_when_loaded_task_has_no_ref() -> None:
    from gobby.dispatch.actions import SpawnAgentAction

    action = _evaluate(
        _task_at(
            "ideation",
            "in_progress",
            id="2bc4656b-f91a-4434-8272-8167e6cb924b",
            ref=None,
            seq_num=14370,
            title="Docs E2E",
        )
    )

    assert isinstance(action, SpawnAgentAction)
    assert action.task_id == "2bc4656b-f91a-4434-8272-8167e6cb924b"
    assert action.task_ref == "#14370"
    assert "#14370" in action.prompt
    assert "2bc4656b-f91a-4434-8272-8167e6cb924b" not in action.prompt


def test_discovery_artifact_complete_rule_advances_persisted_ideation() -> None:
    from gobby.dispatch.actions import AdvanceStageAction

    action = _evaluate(
        _task_at(
            "ideation",
            "in_progress",
            description=(
                "<!-- gobby:discovery-stage:ideation:start -->\n"
                "## Discovery Brief\n\n"
                "### Problem\n- Problem framed.\n\n"
                "### Constraints\n- Constraint captured.\n\n"
                "### Hypotheses\n- Hypothesis captured.\n\n"
                "### Open Questions\n- None.\n"
                "<!-- gobby:discovery-stage:ideation:end -->"
            ),
        )
    )

    assert isinstance(action, AdvanceStageAction)
    assert (action.stage_name, action.method) == ("ideation", "complete_stage")
    assert action.by_session_id == "dispatcher"


def test_discovery_artifact_complete_rule_requires_marker_headings() -> None:
    from gobby.dispatch.actions import SpawnAgentAction

    action = _evaluate(
        _task_at(
            "ideation",
            "in_progress",
            description=(
                "<!-- gobby:discovery-stage:ideation:start -->\n"
                "## Discovery Brief\n\n"
                "### Problem\n- Problem framed.\n"
                "<!-- gobby:discovery-stage:ideation:end -->"
            ),
        )
    )

    assert isinstance(action, SpawnAgentAction)
    assert action.agent_slug == "analyst"


def test_discovery_artifact_complete_rule_validates_architecture_test_section() -> None:
    from gobby.dispatch.actions import SpawnAgentAction

    action = _evaluate(
        _task_at(
            "architecture",
            "in_progress",
            description=(
                "<!-- gobby:discovery-stage:architecture:start -->\n"
                "## Architecture Brief\n\n"
                "### Drivers\n- Driver.\n\n"
                "### Decisions\n- Decision.\n\n"
                "### Components\n- Component.\n\n"
                "### Interfaces\n- Interface.\n\n"
                "### Trade-offs\n- Trade-off.\n\n"
                "### Open Questions\n- None.\n"
                "<!-- gobby:discovery-stage:architecture:end -->"
            ),
        )
    )

    assert isinstance(action, SpawnAgentAction)
    assert action.agent_slug == "architect"


def test_expansion_work_rule_fires_and_holds_when_cap_reached() -> None:
    from gobby.dispatch.actions import StartPipelineAction

    action = _evaluate(_task_at("expansion", "in_progress"))
    assert isinstance(action, StartPipelineAction)
    assert action.pipeline_name == "02e3e743-e572-51b3-a0f4-83e68271282f"
    assert action.dispatch_inputs == {"task_id": "${{ task_id }}"}

    capped = _evaluate(
        _task_at(
            "expansion",
            "in_progress",
            stage_overrides={"work_attempt_count": 3, "max_work_attempts": 3},
        )
    )
    assert isinstance(capped, StartPipelineAction)

    exhausted = _evaluate(
        _task_at(
            "expansion",
            "in_progress",
            stage_overrides={"work_attempt_count": 4, "max_work_attempts": 3},
        )
    )
    assert exhausted is None


def test_development_work_rule_allows_first_counted_attempt_at_cap() -> None:
    from gobby.dispatch.actions import SpawnAgentAction

    action = _evaluate(
        _task_at(
            "development",
            "in_progress",
            stage_overrides={"work_attempt_count": 1, "max_work_attempts": 1},
        )
    )

    assert isinstance(action, SpawnAgentAction)
    assert action.agent_slug == "backend-developer"


def test_planning_work_rule_uses_review_budget_for_revisions() -> None:
    from gobby.dispatch.actions import SpawnAgentAction

    action = _evaluate(
        _task_at(
            "planning",
            "in_progress",
            stage_overrides={
                "work_attempt_count": 6,
                "max_work_attempts": 3,
                "review_round_count": 5,
                "max_review_rounds": 99,
            },
        )
    )

    assert isinstance(action, SpawnAgentAction)
    assert action.agent_slug == "planner"


def test_planning_work_rule_escalates_when_review_budget_is_exhausted() -> None:
    from gobby.dispatch.actions import EscalateAction

    action = _evaluate(
        _task_at(
            "planning",
            "in_progress",
            stage_overrides={
                "work_attempt_count": 4,
                "max_work_attempts": 3,
                "review_round_count": 2,
                "max_review_rounds": 2,
            },
        )
    )

    assert isinstance(action, EscalateAction)
    assert action.reason == "planning_max_work_attempts"


def test_planning_work_rule_escalates_at_review_budget_boundary() -> None:
    from gobby.dispatch.actions import EscalateAction

    action = _evaluate(
        _task_at(
            "planning",
            "in_progress",
            stage_overrides={
                "work_attempt_count": 4,
                "max_work_attempts": 3,
                "review_round_count": 4,
                "max_review_rounds": 99,
            },
        )
    )

    assert isinstance(action, EscalateAction)
    assert action.reason == "planning_max_work_attempts"


def test_development_rule_falls_back_from_missing_assigned_agent() -> None:
    from gobby.dispatch.actions import SpawnAgentAction

    action = _evaluate(_task_at("development", "in_progress", assigned_agent="test-architect"))

    assert isinstance(action, SpawnAgentAction)
    assert action.agent_slug == "backend-developer"
    assert "Follow the developer agent contract" in action.prompt
    assert "default.yaml agent" not in action.prompt


def test_non_epic_with_stale_children_is_still_a_development_leaf() -> None:
    from gobby.dispatch.actions import SpawnAgentAction

    task = _task_at("development", "in_progress")
    task.children = [SimpleNamespace(id="stale-child")]

    assert isinstance(_evaluate(task), SpawnAgentAction)


def test_development_rule_falls_back_from_agent_without_prompt_builder() -> None:
    from gobby.dispatch.actions import SpawnAgentAction

    action = _evaluate(
        _task_at("development", "in_progress", assigned_agent="custom-agent"),
        _context(
            agents=_agents(
                **{
                    "custom-agent": SimpleNamespace(name="custom-agent", enabled=True),
                }
            )
        ),
    )

    assert isinstance(action, SpawnAgentAction)
    assert action.agent_slug == "backend-developer"


@pytest.mark.parametrize(
    ("implementation_domain", "agent_slug"),
    [
        ("backend", "backend-developer"),
        ("frontend", "frontend-developer"),
        ("fullstack", "fullstack-developer"),
    ],
)
def test_development_rule_routes_code_by_implementation_domain(
    implementation_domain: str,
    agent_slug: str,
) -> None:
    from gobby.dispatch.actions import SpawnAgentAction

    action = _evaluate(
        _task_at(
            "development",
            "in_progress",
            category="code",
            assigned_agent=None,
            implementation_domain=implementation_domain,
        )
    )

    assert isinstance(action, SpawnAgentAction)
    assert action.agent_slug == agent_slug


def test_development_rule_falls_back_from_disabled_implementation_domain_agent(
    caplog: pytest.LogCaptureFixture,
) -> None:
    from gobby.dispatch.actions import SpawnAgentAction

    action = _evaluate(
        _task_at(
            "development",
            "in_progress",
            category="code",
            assigned_agent=None,
            implementation_domain="frontend",
        ),
        _context(
            agents=_agents(
                **{
                    "frontend-developer": SimpleNamespace(
                        name="frontend-developer",
                        enabled=False,
                    )
                }
            )
        ),
    )

    assert isinstance(action, SpawnAgentAction)
    assert action.agent_slug == "backend-developer"
    assert "Ignoring unavailable implementation-domain development agent" in caplog.text


def test_development_rule_escalates_when_domain_and_fallback_agents_unavailable() -> None:
    from gobby.dispatch.actions import EscalateAction

    action = _evaluate(
        _task_at(
            "development",
            "in_progress",
            category="code",
            assigned_agent=None,
            implementation_domain="frontend",
        ),
        _context(
            agents=_agents(
                **{
                    "backend-developer": None,
                    "frontend-developer": SimpleNamespace(
                        name="frontend-developer",
                        enabled=False,
                    ),
                }
            )
        ),
    )

    assert isinstance(action, EscalateAction)
    assert action.reason == "development_no_agent"


def test_expansion_review_rule_escalates_when_review_cap_reached() -> None:
    from gobby.dispatch.actions import EscalateAction

    capped = _evaluate(
        _task_at(
            "expansion",
            "needs_review",
            stage_overrides={"review_round_count": 2, "max_review_rounds": 2},
        )
    )

    assert isinstance(capped, EscalateAction)
    assert capped.reason == "expansion_max_review_rounds"


def test_isolation_rule_starts_development_and_defers_workspace_to_spawn() -> None:
    from gobby.dispatch.actions import StartStageAction

    action = _evaluate(
        _task_at("development", "ready", isolation="worktree"),
        _context(artifacts=_artifacts()),
    )

    assert isinstance(action, StartStageAction)
    assert action.stage_name == "development"


def test_isolation_rule_starts_development_when_isolation_none() -> None:
    from gobby.dispatch.actions import StartStageAction

    action = _evaluate(_task_at("development", "ready", isolation="none"))

    assert isinstance(action, StartStageAction)
    assert action.stage_name == "development"


def test_dev_rule_blocked_by_missing_isolation_artifacts() -> None:
    action = _evaluate(
        _task_at("development", "ready", isolation="worktree"),
        _context(artifacts=_artifacts()),
    )

    assert action is not None


def test_development_ready_with_dependency_does_not_start_workspace() -> None:
    action = _evaluate(
        _task_at(
            "development",
            "ready",
            isolation="worktree",
            active_blocked_by={"27f6003c-1540-5098-9dfc-272e9497ba0e"},
        ),
        _context(artifacts=_artifacts()),
    )

    assert action is None


def test_dev_rule_fires_after_stage_start() -> None:
    from gobby.dispatch.actions import SpawnAgentAction

    action = _evaluate(
        _task_at("development", "in_progress", isolation="worktree"),
        _context(artifacts=_artifacts(worktree_path="/tmp/wt", base_commit_sha="abc")),
    )

    assert isinstance(action, SpawnAgentAction)
    assert action.agent_slug == "backend-developer"


def test_non_root_leaf_merge_uses_workspace_merge_action() -> None:
    from gobby.dispatch.actions import MergeWorkspaceAction

    action = _evaluate(
        _task_at("merge", "in_progress", parent_task_id="epic-1"),
        _context(
            artifacts=_artifacts(
                worktree_id="6a061cb3-f607-55f6-b3eb-04579360a44c", target_branch="integration/root"
            )
        ),
    )

    assert isinstance(action, MergeWorkspaceAction)
    assert action.backend == "worktree"
    assert action.source_workspace_id == "6a061cb3-f607-55f6-b3eb-04579360a44c"
    assert action.target_branch == "integration/root"


def test_workspace_merge_conflict_label_routes_to_merge_orchestrator() -> None:
    from gobby.dispatch.actions import SpawnAgentAction
    from gobby.dispatch.merge_recovery import WORKSPACE_MERGE_CONFLICT_LABEL

    action = _evaluate(
        _task_at(
            "merge",
            "in_progress",
            parent_task_id="epic-1",
            labels=[WORKSPACE_MERGE_CONFLICT_LABEL],
        ),
        _context(
            artifacts=_artifacts(
                worktree_id="6a061cb3-f607-55f6-b3eb-04579360a44c", target_branch="integration/root"
            )
        ),
    )

    assert isinstance(action, SpawnAgentAction)
    assert action.agent_slug == "merge-orchestrator"


def test_root_merge_still_routes_to_merge_orchestrator() -> None:
    from gobby.dispatch.actions import SpawnAgentAction

    action = _evaluate(
        _task_at("merge", "in_progress"),
        _context(
            artifacts=_artifacts(
                worktree_id="6a061cb3-f607-55f6-b3eb-04579360a44c", target_branch="main"
            )
        ),
    )

    assert isinstance(action, SpawnAgentAction)
    assert action.agent_slug == "merge-orchestrator"


def test_root_epic_integration_workspace_uses_workspace_merge_action() -> None:
    from gobby.dispatch.actions import MergeWorkspaceAction

    action = _evaluate(
        _task_at("merge", "in_progress", task_type="epic"),
        _context(
            artifacts=_artifacts(
                integration_branch="gobby/integration/root",
                integration_workspace_id="b66893a5-2583-56e1-a0b4-0c96e5bc6917",
                target_branch="main",
            )
        ),
    )

    assert isinstance(action, MergeWorkspaceAction)
    assert action.backend == "worktree"
    assert action.source_workspace_id == "b66893a5-2583-56e1-a0b4-0c96e5bc6917"
    assert action.source_branch == "gobby/integration/root"
    assert action.target_branch == "main"


def test_docs_dev_rule_routes_to_tech_writer_when_available() -> None:
    from gobby.dispatch.actions import SpawnAgentAction

    action = _evaluate(
        _task_at(
            "development",
            "in_progress",
            category="docs",
            assigned_agent=None,
        ),
        _context(
            agents=_agents(**{"tech-writer": SimpleNamespace(name="tech-writer", enabled=True)})
        ),
    )

    assert isinstance(action, SpawnAgentAction)
    assert action.agent_slug == "tech-writer"


def test_docs_dev_rule_falls_back_when_tech_writer_is_disabled() -> None:
    from gobby.dispatch.actions import SpawnAgentAction

    action = _evaluate(
        _task_at(
            "development",
            "in_progress",
            category="docs",
            assigned_agent=None,
        ),
        _context(
            agents=_agents(
                **{
                    "tech-writer": SimpleNamespace(
                        name="tech-writer",
                        enabled=False,
                    )
                }
            )
        ),
    )

    assert isinstance(action, SpawnAgentAction)
    assert action.agent_slug == "backend-developer"


def test_qa_rule_fires_with_cap() -> None:
    from gobby.dispatch.actions import EscalateAction, SpawnAgentAction

    action = _evaluate(_task_at("development", "needs_review"))
    assert isinstance(action, SpawnAgentAction)
    assert action.agent_slug == "qa-reviewer"

    capped = _evaluate(
        _task_at(
            "development",
            "needs_review",
            stage_overrides={"review_round_count": 2, "max_review_rounds": 2},
        )
    )
    assert isinstance(capped, EscalateAction)
    assert capped.reason == "development_max_review_rounds"


def test_development_review_uses_snapshot_reviewer_agent() -> None:
    from gobby.dispatch.actions import SpawnAgentAction

    action = _evaluate(
        _task_at(
            "development",
            "needs_review",
            category="docs",
            stage_overrides={"reviewer_agent": "doc-reviewer"},
        )
    )

    assert isinstance(action, SpawnAgentAction)
    assert action.agent_slug == "doc-reviewer"


def test_development_review_escalates_when_snapshot_reviewer_disabled() -> None:
    from gobby.dispatch.actions import EscalateAction

    action = _evaluate(
        _task_at(
            "development",
            "needs_review",
            stage_overrides={"reviewer_agent": "doc-reviewer"},
        ),
        _context(agents=_agents(**{"doc-reviewer": SimpleNamespace(enabled=False)})),
    )

    assert isinstance(action, EscalateAction)
    assert action.reason == "development_no_reviewer"


def test_leaf_park_rule_completes_review_approved_development_stage() -> None:
    from gobby.dispatch.actions import AdvanceStageAction

    action = _evaluate(_task_at("development", "review_approved"))

    assert isinstance(action, AdvanceStageAction)
    assert (action.stage_name, action.method) == ("development", "complete_stage")


def test_all_leaves_epic_rule_starts_epic_when_leaves_parked() -> None:
    from gobby.dispatch.actions import StartStageAction

    child = _task(stages=[_stage("development", "done")])
    action = _evaluate(
        _task_at("epic_qa", "ready", task_type="epic"),
        _context(children=[child]),
    )

    assert isinstance(action, StartStageAction)
    assert action.stage_name == "epic_qa"


def test_all_leaves_epic_rule_holds_while_leaves_in_flight() -> None:
    child = _task_at("development", "in_progress")

    assert (
        _evaluate(
            _task_at("epic_qa", "ready", task_type="epic"),
            _context(children=[child]),
        )
        is None
    )


def test_epic_descendant_gate_rule_appends_marker_once() -> None:
    from gobby.dispatch.actions import AppendAuditMarkerAction

    gate = SimpleNamespace(
        blockers=(
            SimpleNamespace(
                task_id="46a005df-b318-5d7b-a5b5-9b843d64909d",
                task_ref="#2",
                task_path="1.2",
                title="Reopened child",
                stage_name="development",
                stage_state="ready",
                is_escalated=False,
            ),
        )
    )
    context = _context(
        children=[_task(stages=[])],
        epic_descendant_gate=gate,
    )
    task = _task_at("epic_qa", "ready", task_type="epic")

    action = _evaluate(task, context)

    assert isinstance(action, AppendAuditMarkerAction)
    assert action.heading == "Epic QA deferred"
    assert "#2" in action.body
    assert "stage=development:ready" in action.body

    description = f"\n\n### {action.heading}\n\n{action.body}"
    repeated_task = _task_at(
        "epic_qa",
        "ready",
        task_type="epic",
        description=description,
    )
    assert _evaluate(repeated_task, context) is None

    for stage_state in ("in_progress", "needs_review", "review_approved"):
        changed_gate = SimpleNamespace(
            blockers=(
                SimpleNamespace(
                    task_id="46a005df-b318-5d7b-a5b5-9b843d64909d",
                    task_ref="#2",
                    task_path="1.2",
                    title="Reopened child",
                    stage_name="development",
                    stage_state=stage_state,
                    is_escalated=False,
                ),
            )
        )
        changed_context = _context(
            children=[_task(stages=[])],
            epic_descendant_gate=changed_gate,
        )
        assert _evaluate(repeated_task, changed_context) is None


def test_all_leaves_epic_rule_never_targets_merging_directly() -> None:
    action = _evaluate(
        _task_at("epic_qa", "ready", task_type="epic"),
        _context(children=[_task(stages=[_stage("development", "done")])]),
    )

    assert getattr(action, "stage_name", None) != "merge"


def test_epic_development_starts_as_parking_stage() -> None:
    from gobby.dispatch.actions import StartStageAction

    child = _task_at("development", "in_progress")
    action = _evaluate(
        _task_at("development", "ready", task_type="epic"),
        _context(children=[child]),
    )

    assert isinstance(action, StartStageAction)
    assert action.stage_name == "development"


def test_epic_development_completes_when_leaves_are_parked() -> None:
    from gobby.dispatch.actions import AdvanceStageAction

    child = _task(stages=[_stage("development", "done")])
    action = _evaluate(
        _task_at("development", "in_progress", task_type="epic"),
        _context(children=[child]),
    )

    assert isinstance(action, AdvanceStageAction)
    assert (action.stage_name, action.method) == ("development", "complete_stage")
    assert action.validation_override_reason == "children_parked"


def test_epic_rule_fires_when_stage_is_in_progress() -> None:
    from gobby.dispatch.actions import SpawnAgentAction

    action = _evaluate(
        _task_at("epic_qa", "in_progress", task_type="epic"),
        _context(children=[_task(stages=[_stage("development", "done")])]),
    )

    assert isinstance(action, SpawnAgentAction)
    assert action.agent_slug == "epic-reviewer"


def test_pr_rule_routes_to_merge_orchestrator() -> None:
    from gobby.dispatch.actions import SpawnAgentAction

    action = _evaluate(_task_at("pr", "in_progress", task_type="epic"))

    assert isinstance(action, SpawnAgentAction)
    assert action.agent_slug == "merge-orchestrator"


def test_review_dispatch_remains_single_existing_agent_per_stage() -> None:
    from gobby.dispatch.actions import SpawnAgentAction
    from gobby.dispatch.rules import BASE_RULES, RULES

    scenarios = [
        (BASE_RULES, _task_at("development", "needs_review"), "qa-reviewer"),
        (
            BASE_RULES,
            _task_at("epic_qa", "in_progress", task_type="epic"),
            "epic-reviewer",
        ),
        (BASE_RULES, _task_at("pr", "in_progress", task_type="epic"), "merge-orchestrator"),
        (RULES, _task_at("merge", "in_progress", task_type="epic"), "merge-orchestrator"),
    ]

    for rule_set, task, expected_agent in scenarios:
        actions = [
            action
            for rule in rule_set
            if isinstance((action := rule(task, _context())), SpawnAgentAction)
        ]

        assert [action.agent_slug for action in actions] == [expected_agent]


def test_pr_rule_escalates_when_merge_orchestrator_missing() -> None:
    from gobby.dispatch.actions import EscalateAction

    action = _evaluate(
        _task_at("pr", "in_progress", task_type="epic"),
        _context(agents=_agents(**{"merge-orchestrator": None})),
    )

    assert isinstance(action, EscalateAction)
    assert action.reason == "pr_no_agent"


@pytest.mark.parametrize(
    "stage_name",
    [
        "planning",
        "expansion",
        "development",
        "epic_qa",
        "pr",
    ],
)
def test_review_approved_stages_complete_stage(stage_name: str) -> None:
    from gobby.dispatch.actions import AdvanceStageAction

    action = _evaluate(_task_at(stage_name, "review_approved"))

    assert isinstance(action, AdvanceStageAction)
    assert (action.stage_name, action.method) == (stage_name, "complete_stage")


def test_attended_review_cap_escalates_with_reason() -> None:
    from gobby.dispatch.actions import EscalateAction

    action = _evaluate(
        _task_at(
            "development",
            "needs_review",
            stage_overrides={"review_round_count": 2, "max_review_rounds": 2},
        )
    )

    assert isinstance(action, EscalateAction)
    assert action.reason == "development_max_review_rounds"


def test_epic_qa_review_escalates_when_review_cap_reached() -> None:
    from gobby.dispatch.actions import EscalateAction

    action = _evaluate(
        _task_at(
            "epic_qa",
            "needs_review",
            stage_overrides={"review_round_count": 2, "max_review_rounds": 2},
        )
    )

    assert isinstance(action, EscalateAction)
    assert action.reason == "epic_qa_max_review_rounds"


def test_base_rules_order_excludes_merge_rule() -> None:
    from gobby.dispatch.rules import BASE_RULES

    assert [rule.__name__ for rule in BASE_RULES] == [
        "auto_advance_ready_rule",
        "disabled_agent_escalation_rule",
        "development_isolation_rule",
        "epic_descendant_gate_rule",
        "all_leaves_epic_rule",
        "epic_development_start_rule",
        "epic_development_complete_rule",
        "discovery_artifact_complete_rule",
        "ideation_rule",
        "research_rule",
        "architecture_rule",
        "prd_rule",
        "planning_work_rule",
        "planning_enhancement_rule",
        "planning_review_rule",
        "planning_advance_rule",
        "expansion_work_rule",
        "expansion_review_rule",
        "expansion_advance_rule",
        "development_rule",
        "development_review_rule",
        "development_advance_rule",
        "epic_qa_rule",
        "epic_qa_review_rule",
        "epic_qa_advance_rule",
        "pr_work_rule",
        "pr_review_rule",
        "pr_advance_rule",
    ]
    assert "merge_rule" not in {rule.__name__ for rule in BASE_RULES}


def test_final_rules_is_base_rules_plus_merge_rule_at_final_position() -> None:
    from gobby.dispatch.rules import BASE_RULES, RULES, merge_rule

    assert RULES == [*BASE_RULES, merge_rule]
    assert len(RULES) == 29
    assert RULES[-1] is merge_rule


def test_merge_rule_routes_on_merge_stage() -> None:
    from gobby.dispatch.actions import SpawnAgentAction
    from gobby.dispatch.rules import merge_rule

    action = merge_rule(_task_at("merge", "in_progress", task_type="epic"), _context())

    assert isinstance(action, SpawnAgentAction)
    assert action.agent_slug == "merge-orchestrator"


def test_merge_rule_does_not_advance_stage() -> None:
    from gobby.dispatch.actions import AdvanceStageAction
    from gobby.dispatch.rules import merge_rule

    action = merge_rule(_task_at("merge", "in_progress", task_type="epic"), _context())

    assert not isinstance(action, AdvanceStageAction)


def test_merge_rule_escalates_when_merge_agent_missing() -> None:
    from gobby.dispatch.actions import EscalateAction
    from gobby.dispatch.rules import merge_rule

    action = merge_rule(
        _task_at("merge", "in_progress", task_type="epic"),
        _context(agents=_agents(**{"merge-orchestrator": None})),
    )

    assert isinstance(action, EscalateAction)
    assert action.reason == "merge_no_agent"
