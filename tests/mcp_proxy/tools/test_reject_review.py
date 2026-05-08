"""Tests for reject_review MCP tool."""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from gobby.storage.tasks import Task
from gobby.storage.tasks._stage_states import StageState
from gobby.utils.session_context import session_context_for_test

pytestmark = pytest.mark.unit


@pytest.fixture
def mock_task_manager():
    manager = MagicMock()
    manager.db = MagicMock()
    manager.stage_states = MagicMock()
    manager.stage_states.get.return_value = _stage_state(
        "550e8400-e29b-41d4-a716-446655440000",
        state="ready",
    )
    return manager


def _stage_state(
    task_id: str,
    *,
    stage_name: str = "planning",
    state: str = "ready",
) -> StageState:
    return StageState(
        task_id=task_id,
        stage_name=stage_name,
        position=0,
        state=state,  # type: ignore[arg-type]
        review_policy="required",
        reviewer_agent="plan-adversary",
        entered_at=None,
        entered_by_session_id=None,
        completed_at=None,
        completed_by_session_id=None,
        completed_commit_sha=None,
        work_attempt_count=1,
        review_round_count=1,
        max_work_attempts=None,
        max_review_rounds=None,
        artifact_refs=None,
        notes=None,
        updated_at="2024-01-01T00:00:00Z",
    )


def _task(
    *,
    status: str,
    description: str | None = None,
    labels: list[str] | None = None,
    seq_num: int | None = None,
    assignee: str | None = None,
    claimed_by_session_id: str | None = None,
) -> Task:
    stage_state = {"open": "ready"}.get(status, status)
    return Task(
        id="550e8400-e29b-41d4-a716-446655440000",
        project_id="proj-1",
        title="Planning Task",
        priority=2,
        task_type="task",
        created_at="2024-01-01T00:00:00Z",
        updated_at="2024-01-01T00:00:00Z",
        description=description,
        labels=labels,
        seq_num=seq_num,
        assignee=assignee,
        claimed_by_session_id=claimed_by_session_id,
        stages=(
            {
                "stage_name": "planning",
                "position": 0,
                "state": stage_state,
            },
        ),
    )


@pytest.fixture
def mock_sync_manager():
    return MagicMock()


@pytest.fixture
def sample_task_needs_review():
    return _task(
        status="needs_review",
        description="Existing description",
        labels=["planning-round:0"],
        seq_num=42,
    )


@pytest.fixture
def sample_task_open():
    return _task(status="open")


@pytest.fixture
def sample_task_in_progress():
    return _task(
        status="in_progress",
        description="Existing description",
        labels=["planning-round:0"],
        seq_num=42,
        claimed_by_session_id="session-abc",
        assignee="session-abc",
    )


@pytest.fixture
def stage_ops_registry(mock_task_manager, mock_sync_manager):
    from gobby.mcp_proxy.tools.tasks._context import RegistryContext
    from gobby.mcp_proxy.tools.tasks._stage_ops import create_stage_ops_registry

    ctx = RegistryContext(
        task_manager=mock_task_manager,
        sync_manager=mock_sync_manager,
    )
    ctx.resolve_session_id = MagicMock(return_value="resolved-session-abc")
    ctx.session_manager = MagicMock()
    ctx.session_manager.get.return_value = None
    ctx.session_task_manager = MagicMock()
    ctx.session_var_manager = MagicMock()
    ctx.get_project_repo_path = MagicMock(return_value=None)
    return create_stage_ops_registry(ctx), ctx


class TestMarkTaskReviewRejected:
    @pytest.fixture(autouse=True)
    def _set_session_context(self):
        with session_context_for_test("session-abc"):
            yield

    @pytest.mark.asyncio
    async def test_reject_needs_review_task(
        self, stage_ops_registry, mock_task_manager, sample_task_needs_review
    ) -> None:
        registry, ctx = stage_ops_registry
        mock_task_manager.get_task.side_effect = (
            lambda task_id: sample_task_needs_review
            if task_id == sample_task_needs_review.id
            else None
        )
        rejected_task = _task(
            status="open",
            labels=["planning-round:1"],
            description=sample_task_needs_review.description,
            seq_num=sample_task_needs_review.seq_num,
        )
        mock_task_manager.reject_review.return_value = rejected_task

        with (
            patch(
                "gobby.mcp_proxy.tools.tasks._stage_review.resolve_task_id_for_mcp",
                return_value=sample_task_needs_review.id,
            ),
            patch("gobby.mcp_proxy.tools.tasks._stage_review.notify_parent_on_task_state_change"),
        ):
            result = await registry.call(
                "reject_review",
                {
                    "task_id": "#42",
                    "stage_name": "planning",
                    "rejection_notes": "Need better sequencing",
                    "round_number": 1,
                },
            )

        assert "error" not in result
        mock_task_manager.reject_review.assert_called_once_with(
            sample_task_needs_review.id,
            "planning",
            rejection_notes="Need better sequencing",
            round_number=1,
            by_session_id="resolved-session-abc",
        )
        ctx.session_task_manager.link_task.assert_called_once_with(
            "resolved-session-abc",
            sample_task_needs_review.id,
            "review_rejected",
        )

    @pytest.mark.asyncio
    async def test_reject_in_progress_task(
        self, stage_ops_registry, mock_task_manager, sample_task_in_progress
    ) -> None:
        registry, _ = stage_ops_registry
        mock_task_manager.get_task.side_effect = (
            lambda task_id: sample_task_in_progress
            if task_id == sample_task_in_progress.id
            else None
        )
        rejected_task = _task(
            status="open",
            labels=["planning-round:1"],
            claimed_by_session_id=None,
            assignee=None,
            description=sample_task_in_progress.description,
            seq_num=sample_task_in_progress.seq_num,
        )
        mock_task_manager.reject_review.return_value = rejected_task

        with (
            patch(
                "gobby.mcp_proxy.tools.tasks._stage_review.resolve_task_id_for_mcp",
                return_value=sample_task_in_progress.id,
            ),
            patch("gobby.mcp_proxy.tools.tasks._stage_review.notify_parent_on_task_state_change"),
            patch("gobby.mcp_proxy.tools.tasks._stage_review._clear_prior_claim_session_variables"),
        ):
            result = await registry.call(
                "reject_review",
                {
                    "task_id": "#42",
                    "stage_name": "planning",
                    "rejection_notes": "Need better sequencing",
                    "round_number": 1,
                },
            )

        assert "error" not in result
        mock_task_manager.reject_review.assert_called_once_with(
            sample_task_in_progress.id,
            "planning",
            rejection_notes="Need better sequencing",
            round_number=1,
            by_session_id="resolved-session-abc",
        )

    @pytest.mark.asyncio
    async def test_reject_releases_own_running_agent_dispatch_mutex(
        self, stage_ops_registry, mock_task_manager, sample_task_needs_review
    ) -> None:
        registry, _ = stage_ops_registry
        mock_task_manager.get_task.side_effect = (
            lambda task_id: sample_task_needs_review
            if task_id == sample_task_needs_review.id
            else None
        )
        mock_task_manager.reject_review.return_value = _task(
            status="open",
            labels=["planning-round:1"],
            description=sample_task_needs_review.description,
            seq_num=sample_task_needs_review.seq_num,
        )
        mock_task_manager.db.fetchone.return_value = {"id": "run-1"}
        mutexes = MagicMock()
        mutexes.get_mutex.return_value = SimpleNamespace(run_id="run-1")
        mutexes.clear_by_run_id.return_value = 1

        with (
            patch(
                "gobby.mcp_proxy.tools.tasks._stage_review.resolve_task_id_for_mcp",
                return_value=sample_task_needs_review.id,
            ),
            patch("gobby.mcp_proxy.tools.tasks._stage_review.notify_parent_on_task_state_change"),
            patch(
                "gobby.mcp_proxy.tools.tasks._stage_review.TaskDispatchMutexManager",
                return_value=mutexes,
            ),
        ):
            result = await registry.call(
                "reject_review",
                {
                    "task_id": "#42",
                    "stage_name": "planning",
                    "rejection_notes": "Need better sequencing",
                    "round_number": 1,
                },
            )

        assert "error" not in result
        mock_task_manager.db.fetchone.assert_called_once()
        assert mock_task_manager.db.fetchone.call_args.args[1] == (
            "run-1",
            "resolved-session-abc",
            sample_task_needs_review.id,
        )
        mutexes.clear_by_run_id.assert_called_once_with("run-1")
        mock_task_manager.reject_review.assert_called_once()

    @pytest.mark.asyncio
    async def test_reject_rejects_open_task(
        self, stage_ops_registry, mock_task_manager, sample_task_open
    ) -> None:
        registry, _ = stage_ops_registry
        mock_task_manager.get_task.side_effect = (
            lambda task_id: sample_task_open if task_id == sample_task_open.id else None
        )
        mock_task_manager.reject_review.side_effect = ValueError("Cannot reject review")

        with patch(
            "gobby.mcp_proxy.tools.tasks._stage_review.resolve_task_id_for_mcp",
            return_value=sample_task_open.id,
        ):
            result = await registry.call(
                "reject_review",
                {"task_id": "#42", "stage_name": "planning"},
            )

        assert "error" in result
        assert "Cannot reject review" in result["error"]
        mock_task_manager.reject_review.assert_called_once()

    @pytest.mark.asyncio
    async def test_reject_writes_explicit_signoff_summary_to_session_var(
        self, stage_ops_registry, mock_task_manager, sample_task_needs_review
    ) -> None:
        registry, ctx = stage_ops_registry
        mock_task_manager.get_task.side_effect = (
            lambda task_id: sample_task_needs_review
            if task_id == sample_task_needs_review.id
            else None
        )
        mock_task_manager.reject_review.return_value = _task(
            status="open",
            labels=["planning-round:7"],
            description=sample_task_needs_review.description,
            seq_num=sample_task_needs_review.seq_num,
        )
        with (
            patch(
                "gobby.mcp_proxy.tools.tasks._stage_review.resolve_task_id_for_mcp",
                return_value=sample_task_needs_review.id,
            ),
            patch("gobby.mcp_proxy.tools.tasks._stage_review.notify_parent_on_task_state_change"),
        ):
            result = await registry.call(
                "reject_review",
                {
                    "task_id": "#42",
                    "stage_name": "planning",
                    "rejection_notes": "stuff",
                    "round_number": 7,
                    "signoff_summary": "REJECTED: round 7, 2 blocking (alpha, beta)",
                },
            )
        assert "error" not in result
        ctx.session_var_manager.set_variable.assert_called_once_with(
            "resolved-session-abc",
            "adversary_verdict",
            "REJECTED: round 7, 2 blocking (alpha, beta)",
        )

    @pytest.mark.asyncio
    async def test_reject_synthesizes_stock_signoff_when_omitted(
        self, stage_ops_registry, mock_task_manager, sample_task_needs_review
    ) -> None:
        registry, ctx = stage_ops_registry
        mock_task_manager.get_task.side_effect = (
            lambda task_id: sample_task_needs_review
            if task_id == sample_task_needs_review.id
            else None
        )
        mock_task_manager.reject_review.return_value = _task(
            status="open",
            labels=["planning-round:3"],
            description=sample_task_needs_review.description,
            seq_num=sample_task_needs_review.seq_num,
        )
        with (
            patch(
                "gobby.mcp_proxy.tools.tasks._stage_review.resolve_task_id_for_mcp",
                return_value=sample_task_needs_review.id,
            ),
            patch("gobby.mcp_proxy.tools.tasks._stage_review.notify_parent_on_task_state_change"),
        ):
            result = await registry.call(
                "reject_review",
                {
                    "task_id": "#42",
                    "stage_name": "planning",
                    "rejection_notes": "stuff",
                    "round_number": 3,
                },
            )
        assert "error" not in result
        ctx.session_var_manager.set_variable.assert_called_once_with(
            "resolved-session-abc",
            "adversary_verdict",
            "Rejected #42 round 3",
        )
