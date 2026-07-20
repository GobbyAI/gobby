"""Tests for the record_plan_enhancement MCP tool wrapper."""

from unittest.mock import MagicMock, patch

import pytest

from gobby.storage.tasks import Task
from gobby.storage.tasks._stage_states import StageState
from gobby.utils.session_context import session_context_for_test

pytestmark = pytest.mark.unit

_TASK_ID = "550e8400-e29b-41d4-a716-446655440000"


def _stage_state(state: str = "needs_review") -> StageState:
    return StageState(
        task_id=_TASK_ID,
        stage_name="planning",
        position=0,
        state=state,
        review_policy="required",
        reviewer_agent="plan-adversary",
        entered_at=None,
        entered_by_session_id=None,
        completed_at=None,
        completed_by_session_id=None,
        completed_commit_sha=None,
        work_attempt_count=1,
        review_round_count=0,
        max_work_attempts=None,
        max_review_rounds=None,
        artifact_refs=None,
        notes=None,
        updated_at="2024-01-01T00:00:00Z",
    )


def _task(*, state: str = "needs_review", claimed_by_session_id: str | None = None) -> Task:
    return Task(
        id=_TASK_ID,
        project_id="11111111-1111-4111-8111-111111110001",
        title="Planning Task",
        priority=2,
        task_type="task",
        created_at="2024-01-01T00:00:00Z",
        updated_at="2024-01-01T00:00:00Z",
        description="Plan body",
        seq_num=42,
        claimed_by_session_id=claimed_by_session_id,
        stages=({"stage_name": "planning", "position": 0, "state": state},),
    )


@pytest.fixture
def mock_task_manager():
    manager = MagicMock()
    manager.db = MagicMock()
    manager.stage_states = MagicMock()
    manager.stage_states.get.return_value = _stage_state()
    return manager


@pytest.fixture
def stage_ops_registry(mock_task_manager):
    from gobby.mcp_proxy.tools.tasks._context import RegistryContext
    from gobby.mcp_proxy.tools.tasks._stage_ops import create_stage_ops_registry

    ctx = RegistryContext(
        task_manager=mock_task_manager,
    )
    ctx.resolve_session_id = MagicMock(return_value="resolved-session-abc")
    ctx.session_manager = MagicMock()
    ctx.session_manager.get.return_value = None
    ctx.session_task_manager = MagicMock()
    ctx.session_var_manager = MagicMock()
    ctx.get_project_repo_path = MagicMock(return_value=None)
    return create_stage_ops_registry(ctx), ctx


@pytest.fixture(autouse=True)
def _session_context():
    with session_context_for_test("session-abc"):
        yield


@pytest.mark.asyncio
async def test_record_with_suggestions_routes_to_ready(stage_ops_registry, mock_task_manager):
    registry, ctx = stage_ops_registry
    sample = _task(claimed_by_session_id="session-abc")
    mock_task_manager.get_task.side_effect = (
        lambda task_id: sample if task_id == sample.id else None
    )
    mock_task_manager.record_plan_enhancement.return_value = _task(
        state="ready", claimed_by_session_id=None
    )

    with (
        patch(
            "gobby.mcp_proxy.tools.tasks._stage_review.resolve_task_id_for_mcp",
            return_value=sample.id,
        ),
        patch(
            "gobby.mcp_proxy.tools.tasks._stage_review.notify_parent_on_task_state_change"
        ) as notify,
        patch("gobby.mcp_proxy.tools.tasks._stage_review.clear_prior_claim_session_variables"),
    ):
        result = await registry.call(
            "record_plan_enhancement",
            {
                "task_id": "#42",
                "round_number": 1,
                "converged": False,
                "suggestions": ["Tighten acceptance items"],
            },
        )

    assert "error" not in result
    mock_task_manager.record_plan_enhancement.assert_called_once_with(
        sample.id,
        round_number=1,
        converged=False,
        suggestions=["Tighten acceptance items"],
        signoff_summary=None,
        by_session_id="resolved-session-abc",
    )
    # Suggestions route the plan back to the planner (ready).
    ctx.session_task_manager.link_task.assert_called_once_with(
        "resolved-session-abc", sample.id, "ready"
    )
    assert notify.call_args.args[2] == "ready"


@pytest.mark.asyncio
async def test_record_converged_leaves_needs_review(stage_ops_registry, mock_task_manager):
    registry, ctx = stage_ops_registry
    sample = _task(claimed_by_session_id="session-abc")
    mock_task_manager.get_task.side_effect = (
        lambda task_id: sample if task_id == sample.id else None
    )
    mock_task_manager.record_plan_enhancement.return_value = _task(
        state="needs_review", claimed_by_session_id=None
    )

    with (
        patch(
            "gobby.mcp_proxy.tools.tasks._stage_review.resolve_task_id_for_mcp",
            return_value=sample.id,
        ),
        patch(
            "gobby.mcp_proxy.tools.tasks._stage_review.notify_parent_on_task_state_change"
        ) as notify,
        patch("gobby.mcp_proxy.tools.tasks._stage_review.clear_prior_claim_session_variables"),
    ):
        result = await registry.call(
            "record_plan_enhancement",
            {
                "task_id": "#42",
                "round_number": 1,
                "converged": True,
                "suggestions": [],
            },
        )

    assert "error" not in result
    mock_task_manager.record_plan_enhancement.assert_called_once_with(
        sample.id,
        round_number=1,
        converged=True,
        suggestions=[],
        signoff_summary=None,
        by_session_id="resolved-session-abc",
    )
    # A converged round leaves the planning stage in needs_review for the adversary.
    ctx.session_task_manager.link_task.assert_called_once_with(
        "resolved-session-abc", sample.id, "needs_review"
    )
    assert notify.call_args.args[2] == "needs_review"
