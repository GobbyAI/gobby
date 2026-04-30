"""Red tests for ordered dispatcher decision rules."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.unit


def _task(**overrides):
    values = {
        "id": "task-1",
        "ref": "#1",
        "task_type": "task",
        "lifecycle": "in_development",
        "status": "open",
        "labels": [],
        "allow_automation": True,
        "unattended": False,
        "isolation": "none",
        "assigned_agent": "backend-developer",
        "blocked_by": set(),
        "active_blocked_by": set(),
        "validation_fail_count": 0,
        "dispatch_failure_count": 0,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


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
        "max_expansion_attempts": 3,
        "max_qa_rounds": 2,
        "max_dispatch_failures": 3,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _evaluate(task, context=None):
    from gobby.dispatch.rules import evaluate

    return evaluate(task, context or _context())


def test_plan_review_rule_fires_on_plan_review_tuple() -> None:
    from gobby.dispatch.actions import SpawnAgentAction

    action = _evaluate(
        _task(lifecycle="plan_review", status="open"),
        _context(artifacts=_artifacts(plan_file_path=".gobby/plans/task.md")),
    )

    assert isinstance(action, SpawnAgentAction)
    assert action.agent_slug == "plan-reviewer"


def test_test_arch_rule_fires_and_skip_advances() -> None:
    from gobby.dispatch.actions import AdvanceLifecycleAction, SpawnAgentAction

    assert isinstance(_evaluate(_task(lifecycle="test_arch", status="open")), SpawnAgentAction)

    skipped = _evaluate(_task(lifecycle="test_arch", status="open", labels=["stage-:test_arch"]))
    assert isinstance(skipped, AdvanceLifecycleAction)
    assert (skipped.to_lifecycle, skipped.to_status) == ("expanding", "open")


def test_expansion_rule_fires_with_cap() -> None:
    from gobby.dispatch.actions import EscalateAction, StartExpansionAction

    assert isinstance(_evaluate(_task(lifecycle="expanding", status="open")), StartExpansionAction)
    capped = _evaluate(
        _task(lifecycle="expanding", status="open", dispatch_failure_count=3),
        _context(max_expansion_attempts=3),
    )
    assert isinstance(capped, EscalateAction)


def test_isolation_rule_reads_task_isolation_field_and_fires_when_pair_missing() -> None:
    from gobby.dispatch.actions import CreateIsolationAction

    action = _evaluate(_task(isolation="worktree"), _context(artifacts=_artifacts()))

    assert isinstance(action, CreateIsolationAction)
    assert action.isolation == "worktree"


def test_isolation_rule_skips_when_task_isolation_none() -> None:
    from gobby.dispatch.actions import SpawnAgentAction

    assert isinstance(_evaluate(_task(isolation="none")), SpawnAgentAction)


def test_dev_rule_blocked_by_missing_isolation_artifacts() -> None:
    assert _evaluate(_task(isolation="worktree"), _context(artifacts=_artifacts())) is not None


def test_dev_rule_fires_on_unblocked_leaves() -> None:
    from gobby.dispatch.actions import SpawnAgentAction

    action = _evaluate(
        _task(isolation="worktree"),
        _context(artifacts=_artifacts(worktree_path="/tmp/wt", base_commit_sha="abc")),
    )

    assert isinstance(action, SpawnAgentAction)
    assert action.agent_slug == "backend-developer"


def test_qa_rule_fires_with_cap() -> None:
    from gobby.dispatch.actions import EscalateAction, SpawnAgentAction

    assert isinstance(
        _evaluate(_task(lifecycle="in_development", status="needs_review")),
        SpawnAgentAction,
    )
    capped = _evaluate(
        _task(lifecycle="in_development", status="needs_review", validation_fail_count=2),
        _context(max_qa_rounds=2),
    )
    assert isinstance(capped, EscalateAction)


def test_leaf_park_rule_advances_to_holistic_parking() -> None:
    from gobby.dispatch.actions import AdvanceLifecycleAction

    action = _evaluate(_task(lifecycle="in_development", status="review_approved"))

    assert isinstance(action, AdvanceLifecycleAction)
    assert (action.to_lifecycle, action.to_status) == ("holistic_review", "review_approved")


def test_all_leaves_holistic_rule_advances_epic_when_leaves_parked() -> None:
    from gobby.dispatch.actions import AdvanceLifecycleAction

    child = _task(lifecycle="holistic_review", status="review_approved")
    action = _evaluate(
        _task(task_type="epic", lifecycle="in_development", status="open"),
        _context(children=[child]),
    )

    assert isinstance(action, AdvanceLifecycleAction)
    assert (action.to_lifecycle, action.to_status) == ("holistic_review", "open")


def test_all_leaves_holistic_rule_holds_while_leaves_in_flight() -> None:
    child = _task(lifecycle="in_development", status="open")
    assert (
        _evaluate(
            _task(task_type="epic", lifecycle="in_development", status="open"),
            _context(children=[child]),
        )
        is None
    )


def test_all_leaves_holistic_rule_never_targets_merging_directly() -> None:
    action = _evaluate(
        _task(task_type="epic", lifecycle="in_development", status="open"),
        _context(children=[_task(lifecycle="holistic_review", status="review_approved")]),
    )

    assert getattr(action, "to_lifecycle", None) != "merging"


def test_holistic_rule_fires_when_leaves_parked() -> None:
    from gobby.dispatch.actions import SpawnAgentAction

    action = _evaluate(
        _task(task_type="epic", lifecycle="holistic_review", status="open"),
        _context(children=[_task(lifecycle="holistic_review", status="review_approved")]),
    )

    assert isinstance(action, SpawnAgentAction)
    assert action.agent_slug == "reviewer"


def test_pr_rule_attended_escalates_for_pr_creation() -> None:
    from gobby.dispatch.actions import EscalateAction

    action = _evaluate(_task(lifecycle="pr", status="open", unattended=False))

    assert isinstance(action, EscalateAction)
    assert action.reason == "pr_creation_required"


def test_pr_rule_unattended_advances_to_merging() -> None:
    from gobby.dispatch.actions import AdvanceLifecycleAction

    action = _evaluate(_task(lifecycle="pr", status="open", unattended=True))

    assert isinstance(action, AdvanceLifecycleAction)
    assert (action.to_lifecycle, action.to_status) == ("merging", "open")


@pytest.mark.parametrize(
    ("stage", "to_lifecycle", "to_status"),
    [
        ("plan_review", "test_arch", "open"),
        ("test_arch", "expanding", "open"),
        ("expanding", "in_development", "open"),
        ("qa", "holistic_review", "review_approved"),
        ("holistic_review", "pr", "open"),
    ],
)
def test_stage_skips_advance(stage: str, to_lifecycle: str, to_status: str) -> None:
    from gobby.dispatch.actions import AdvanceLifecycleAction

    task = _task(
        lifecycle="in_development" if stage == "qa" else stage,
        status="needs_review" if stage == "qa" else "open",
        labels=[f"stage-:{stage}"],
    )
    action = _evaluate(task)

    assert isinstance(action, AdvanceLifecycleAction)
    assert (action.to_lifecycle, action.to_status) == (to_lifecycle, to_status)


def test_stage_skip_pr_advances_to_merging_under_unattended() -> None:
    from gobby.dispatch.actions import AdvanceLifecycleAction

    action = _evaluate(_task(lifecycle="pr", status="open", labels=["stage-:pr"], unattended=True))

    assert isinstance(action, AdvanceLifecycleAction)
    assert (action.to_lifecycle, action.to_status) == ("merging", "open")


def test_unattended_advances_on_max_retries() -> None:
    from gobby.dispatch.actions import AdvanceLifecycleAction

    action = _evaluate(
        _task(
            lifecycle="in_development",
            status="needs_review",
            unattended=True,
            validation_fail_count=2,
        ),
        _context(max_qa_rounds=2),
    )

    assert isinstance(action, AdvanceLifecycleAction)


def test_attended_escalates_with_reason() -> None:
    from gobby.dispatch.actions import EscalateAction

    action = _evaluate(
        _task(lifecycle="in_development", status="needs_review", validation_fail_count=2),
        _context(max_qa_rounds=2),
    )

    assert isinstance(action, EscalateAction)
    assert action.reason


def test_base_rules_order_excludes_merge_rule() -> None:
    from gobby.dispatch.rules import BASE_RULES

    assert len(BASE_RULES) == 10
    assert BASE_RULES[7].__name__ == "all_leaves_holistic_rule"
    assert "merge_rule" not in {rule.__name__ for rule in BASE_RULES}
