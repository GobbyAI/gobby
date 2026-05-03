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
    "test_arch": "test-architect",
    "development": "backend-developer",
    "holistic_qa": "holistic-reviewer",
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
                "dispatch_target": "expand-task",
                "dispatch_inputs_json": '{"task_id": "${{ task_id }}"}',
            }
        )
    values.update(overrides)
    return SimpleNamespace(**values)


def _agents(**overrides):
    agents = {
        agent_slug: SimpleNamespace(name=agent_slug, enabled=True)
        for agent_slug in {*_STAGE_AGENTS.values(), *_REVIEW_AGENTS.values()}
    }
    agents.update(overrides)
    return agents


def _task(**overrides):
    values = {
        "id": "task-1",
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
        "clone_path": None,
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


def test_test_arch_rule_fires_on_in_progress_stage() -> None:
    from gobby.dispatch.actions import SpawnAgentAction

    action = _evaluate(_task_at("test_arch", "in_progress"))

    assert isinstance(action, SpawnAgentAction)
    assert action.agent_slug == "test-architect"


def test_expansion_work_rule_fires_and_holds_when_cap_reached() -> None:
    from gobby.dispatch.actions import StartPipelineAction

    action = _evaluate(_task_at("expansion", "in_progress"))
    assert isinstance(action, StartPipelineAction)
    assert action.pipeline_name == "expand-task"
    assert action.dispatch_inputs == {"task_id": "${{ task_id }}"}

    capped = _evaluate(
        _task_at(
            "expansion",
            "in_progress",
            stage_overrides={"work_attempt_count": 3, "max_work_attempts": 3},
        )
    )
    assert capped is None


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


def test_isolation_rule_reads_task_isolation_field_and_fires_when_pair_missing() -> None:
    from gobby.dispatch.actions import CreateIsolationAction

    action = _evaluate(
        _task_at("development", "ready", isolation="worktree"),
        _context(artifacts=_artifacts()),
    )

    assert isinstance(action, CreateIsolationAction)
    assert action.isolation == "worktree"


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


def test_dev_rule_fires_after_stage_start() -> None:
    from gobby.dispatch.actions import SpawnAgentAction

    action = _evaluate(
        _task_at("development", "in_progress", isolation="worktree"),
        _context(artifacts=_artifacts(worktree_path="/tmp/wt", base_commit_sha="abc")),
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


def test_leaf_park_rule_completes_review_approved_development_stage() -> None:
    from gobby.dispatch.actions import AdvanceStageAction

    action = _evaluate(_task_at("development", "review_approved"))

    assert isinstance(action, AdvanceStageAction)
    assert (action.stage_name, action.method) == ("development", "complete_stage")


def test_all_leaves_holistic_rule_starts_epic_when_leaves_parked() -> None:
    from gobby.dispatch.actions import StartStageAction

    child = _task(stages=[_stage("development", "done")])
    action = _evaluate(
        _task_at("holistic_qa", "ready", task_type="epic"),
        _context(children=[child]),
    )

    assert isinstance(action, StartStageAction)
    assert action.stage_name == "holistic_qa"


def test_all_leaves_holistic_rule_holds_while_leaves_in_flight() -> None:
    child = _task_at("development", "in_progress")

    assert (
        _evaluate(
            _task_at("holistic_qa", "ready", task_type="epic"),
            _context(children=[child]),
        )
        is None
    )


def test_all_leaves_holistic_rule_never_targets_merging_directly() -> None:
    action = _evaluate(
        _task_at("holistic_qa", "ready", task_type="epic"),
        _context(children=[_task(stages=[_stage("development", "done")])]),
    )

    assert getattr(action, "stage_name", None) != "merge"


def test_holistic_rule_fires_when_stage_is_in_progress() -> None:
    from gobby.dispatch.actions import SpawnAgentAction

    action = _evaluate(
        _task_at("holistic_qa", "in_progress", task_type="epic"),
        _context(children=[_task(stages=[_stage("development", "done")])]),
    )

    assert isinstance(action, SpawnAgentAction)
    assert action.agent_slug == "holistic-reviewer"


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
            _task_at("holistic_qa", "in_progress", task_type="epic"),
            "holistic-reviewer",
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
        "holistic_qa",
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


def test_base_rules_order_excludes_merge_rule() -> None:
    from gobby.dispatch.rules import BASE_RULES

    assert [rule.__name__ for rule in BASE_RULES] == [
        "auto_advance_ready_rule",
        "disabled_agent_escalation_rule",
        "development_isolation_rule",
        "all_leaves_holistic_rule",
        "ideation_rule",
        "research_rule",
        "architecture_rule",
        "prd_rule",
        "planning_work_rule",
        "planning_review_rule",
        "planning_advance_rule",
        "test_arch_rule",
        "expansion_work_rule",
        "expansion_review_rule",
        "expansion_advance_rule",
        "development_rule",
        "development_review_rule",
        "development_advance_rule",
        "holistic_qa_rule",
        "holistic_qa_review_rule",
        "holistic_qa_advance_rule",
        "pr_work_rule",
        "pr_review_rule",
        "pr_advance_rule",
    ]
    assert "merge_rule" not in {rule.__name__ for rule in BASE_RULES}


def test_final_rules_is_base_rules_plus_merge_rule_at_final_position() -> None:
    from gobby.dispatch.rules import BASE_RULES, RULES, merge_rule

    assert RULES == [*BASE_RULES, merge_rule]
    assert len(RULES) == 25
    assert RULES[-1] is merge_rule


def test_merge_rule_routes_on_merge_stage() -> None:
    from gobby.dispatch.actions import SpawnAgentAction
    from gobby.dispatch.rules import merge_rule

    action = merge_rule(_task_at("merge", "in_progress", task_type="epic"), _context())

    assert isinstance(action, SpawnAgentAction)
    assert action.agent_slug == "merge-orchestrator"


def test_merge_rule_does_not_advance_lifecycle() -> None:
    from gobby.dispatch.actions import AdvanceLifecycleAction
    from gobby.dispatch.rules import merge_rule

    action = merge_rule(_task_at("merge", "in_progress", task_type="epic"), _context())

    assert not isinstance(action, AdvanceLifecycleAction)


def test_merge_rule_escalates_when_merge_agent_missing() -> None:
    from gobby.dispatch.actions import EscalateAction
    from gobby.dispatch.rules import merge_rule

    action = merge_rule(
        _task_at("merge", "in_progress", task_type="epic"),
        _context(agents=_agents(**{"merge-orchestrator": None})),
    )

    assert isinstance(action, EscalateAction)
    assert action.reason == "merge_no_agent"
