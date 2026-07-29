"""Tests for approve_review MCP tool and review_approved status.

Tests status transitions, validation, and blocked status in update_task.
"""

import inspect
from types import SimpleNamespace
from typing import Any
from unittest.mock import ANY, MagicMock

import pytest

from gobby.plans.review_evidence_models import ReviewEvidenceError
from gobby.storage.tasks import Task
from gobby.storage.tasks._stage_states import StageState
from gobby.utils.session_context import session_context_for_test

pytestmark = pytest.mark.unit


@pytest.fixture
def mock_task_manager():
    """Create a mock task manager."""
    manager = MagicMock()
    manager.db = MagicMock()
    manager.stage_states = MagicMock()
    manager.stage_states.get.return_value = _stage_state(
        "550e8400-e29b-41d4-a716-446655440000",
        state="review_approved",
    )
    return manager


def _stage_state(
    task_id: str,
    *,
    stage_name: str = "development",
    state: str = "needs_review",
) -> StageState:
    return StageState(
        task_id=task_id,
        stage_name=stage_name,
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


def _task(
    *,
    status: str,
    description: str | None = None,
    seq_num: int | None = None,
) -> Task:
    stage_state = {
        "open": "ready",
        "closed": "done",
    }.get(status, status)
    return Task(
        id="550e8400-e29b-41d4-a716-446655440000",
        project_id="11111111-1111-4111-8111-111111110001",
        title="Test Task",
        priority=2,
        task_type="task",
        created_at="2024-01-01T00:00:00Z",
        updated_at="2024-01-01T00:00:00Z",
        description=description,
        seq_num=seq_num,
        closed_at="2024-01-02T00:00:00Z" if status == "closed" else None,
        stages=(
            {
                "stage_name": "development",
                "position": 0,
                "state": stage_state,
            },
        ),
    )


@pytest.fixture
def sample_task_needs_review():
    """Create a task in needs_review status."""
    return _task(status="needs_review", description="Test description", seq_num=42)


@pytest.fixture
def sample_task_in_progress():
    """Create a task in in_progress status."""
    return _task(status="in_progress", description="Original description", seq_num=42)


@pytest.fixture
def sample_task_open():
    """Create a task in open status."""
    return _task(status="open")


@pytest.fixture
def stage_ops_registry(mock_task_manager):
    """Create a stage ops registry with approve_review tool."""
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
    return create_stage_ops_registry(ctx)


class TestMarkTaskReviewApproved:
    """Tests for approve_review lifecycle tool."""

    @pytest.fixture(autouse=True)
    def _set_session_context(self):
        with session_context_for_test("session-abc"):
            yield

    def test_approve_needs_review_task(
        self, stage_ops_registry, mock_task_manager, sample_task_needs_review
    ) -> None:
        """Test approving a task in needs_review status."""
        mock_task_manager.get_task.return_value = sample_task_needs_review
        approved_task = _task(
            status="review_approved",
            description=sample_task_needs_review.description,
            seq_num=sample_task_needs_review.seq_num,
        )
        mock_task_manager.approve_review.return_value = approved_task

        tool_func = stage_ops_registry._tools["approve_review"].func
        result = tool_func(
            task_id=sample_task_needs_review.id,
            stage_name="development",
        )

        assert "error" not in result
        mock_task_manager.approve_review.assert_called_once_with(
            sample_task_needs_review.id,
            "development",
            approval_notes=None,
            by_session_id=ANY,
        )

    def test_planning_approval_surfaces_server_ledger(
        self,
        stage_ops_registry: Any,
        mock_task_manager: MagicMock,
        sample_task_needs_review: Task,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from gobby.mcp_proxy.tools.tasks import _stage_review

        approval_result = {
            "verdict": "approved",
            "findings": [],
            "manifest_entries": [{"source_section": "1.1"}],
            "routing_decisions": {},
            "coverage_attestation": {"evidence_id": "evidence-1"},
            "quality_ledger": [{"ledger_entry_id": "ledger-server-derived"}],
        }

        class EvidenceService:
            def __init__(self, _db: object) -> None:
                pass

            def get_evidence(self, _evidence_id: str) -> SimpleNamespace:
                return SimpleNamespace(
                    finalized_at=None,
                    approval_result=approval_result,
                )

        monkeypatch.setattr(_stage_review, "PlanReviewEvidenceService", EvidenceService)
        monkeypatch.setattr(
            _stage_review,
            "complete_plan_review_mint",
            lambda *_args, **_kwargs: {"lesson_mint_status": "none"},
        )
        mock_task_manager.get_task.return_value = sample_task_needs_review
        mock_task_manager.approve_review.return_value = sample_task_needs_review
        tool_func = stage_ops_registry._tools["approve_review"].func

        result = tool_func(
            task_id=sample_task_needs_review.id,
            stage_name="planning",
            round_number=1,
            findings=[],
            manifest_entries=[{"source_section": "1.1"}],
            routing_decisions={},
            coverage_attestation={"evidence_id": "evidence-1"},
            evidence_id="evidence-1",
        )

        assert "quality_ledger" not in inspect.signature(tool_func).parameters
        assert result["approval_result"] == approval_result

    def test_planning_approval_replay_reuses_loaded_evidence(
        self,
        stage_ops_registry: Any,
        mock_task_manager: MagicMock,
        sample_task_needs_review: Task,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from gobby.mcp_proxy.tools.tasks import _stage_review

        approval_result = {"verdict": "approved", "quality_ledger": []}

        class EvidenceService:
            calls = 0

            def __init__(self, _db: object) -> None:
                pass

            def get_evidence(self, _evidence_id: str) -> SimpleNamespace:
                type(self).calls += 1
                return SimpleNamespace(
                    finalized_at="2026-07-28T12:00:00Z",
                    approval_result=approval_result,
                )

        monkeypatch.setattr(_stage_review, "PlanReviewEvidenceService", EvidenceService)
        monkeypatch.setattr(
            _stage_review,
            "complete_plan_review_mint",
            lambda *_args, **_kwargs: {"lesson_mint_status": "none"},
        )
        mock_task_manager.get_task.return_value = sample_task_needs_review
        mock_task_manager.approve_review.return_value = sample_task_needs_review
        tool_func = stage_ops_registry._tools["approve_review"].func

        result = tool_func(
            task_id=sample_task_needs_review.id,
            stage_name="planning",
            round_number=1,
            findings=[],
            manifest_entries=[],
            routing_decisions={},
            coverage_attestation={"evidence_id": "evidence-1"},
            evidence_id="evidence-1",
        )

        assert EvidenceService.calls == 1
        assert result["approval_result"] == approval_result

    def test_successful_planning_approval_tolerates_final_evidence_reload_failure(
        self,
        stage_ops_registry: Any,
        mock_task_manager: MagicMock,
        sample_task_needs_review: Task,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from gobby.mcp_proxy.tools.tasks import _stage_review

        class EvidenceService:
            calls = 0

            def __init__(self, _db: object) -> None:
                pass

            def get_evidence(self, _evidence_id: str) -> SimpleNamespace:
                type(self).calls += 1
                if type(self).calls == 1:
                    return SimpleNamespace(finalized_at=None, approval_result=None)
                raise ReviewEvidenceError("evidence_not_found", "evidence disappeared")

        monkeypatch.setattr(_stage_review, "PlanReviewEvidenceService", EvidenceService)
        monkeypatch.setattr(
            _stage_review,
            "complete_plan_review_mint",
            lambda *_args, **_kwargs: {"lesson_mint_status": "none"},
        )
        mock_task_manager.get_task.return_value = sample_task_needs_review
        mock_task_manager.approve_review.return_value = sample_task_needs_review
        tool_func = stage_ops_registry._tools["approve_review"].func

        result = tool_func(
            task_id=sample_task_needs_review.id,
            stage_name="planning",
            round_number=1,
            findings=[],
            manifest_entries=[],
            routing_decisions={},
            coverage_attestation={"evidence_id": "evidence-1"},
            evidence_id="evidence-1",
        )

        assert EvidenceService.calls == 2
        assert "approval_result" not in result
        mock_task_manager.approve_review.assert_called_once()

    def test_approve_in_progress_task(
        self, stage_ops_registry, mock_task_manager, sample_task_in_progress
    ) -> None:
        """Test approving a task in in_progress status (also valid)."""
        mock_task_manager.get_task.return_value = sample_task_in_progress
        mock_task_manager.approve_review.return_value = sample_task_in_progress

        tool_func = stage_ops_registry._tools["approve_review"].func
        result = tool_func(
            task_id=sample_task_in_progress.id,
            stage_name="development",
        )

        assert "error" not in result

    def test_approve_rejects_open_task(
        self, stage_ops_registry, mock_task_manager, sample_task_open
    ) -> None:
        """Open legacy status is delegated to stage-state validation."""
        mock_task_manager.get_task.return_value = sample_task_open
        mock_task_manager.approve_review.side_effect = ValueError("Illegal stage transition")

        tool_func = stage_ops_registry._tools["approve_review"].func
        result = tool_func(
            task_id=sample_task_open.id,
            stage_name="development",
        )

        assert "error" in result
        assert "Illegal stage transition" in result["error"]
        mock_task_manager.approve_review.assert_called_once()

    def test_approve_rejects_closed_task(self, stage_ops_registry, mock_task_manager) -> None:
        """Closed legacy status is delegated to stage-state validation."""
        closed_task = _task(status="closed")
        mock_task_manager.get_task.return_value = closed_task
        mock_task_manager.approve_review.side_effect = ValueError("No current stage")

        tool_func = stage_ops_registry._tools["approve_review"].func
        result = tool_func(
            task_id=closed_task.id,
            stage_name="development",
        )

        assert "error" in result
        assert "No current stage" in result["error"]

    def test_approve_with_notes(
        self, stage_ops_registry, mock_task_manager, sample_task_needs_review
    ) -> None:
        """Test approving with approval notes appends to description."""
        mock_task_manager.get_task.return_value = sample_task_needs_review
        approved_task = _task(
            status="review_approved",
            description=sample_task_needs_review.description,
            seq_num=sample_task_needs_review.seq_num,
        )
        mock_task_manager.approve_review.return_value = approved_task

        tool_func = stage_ops_registry._tools["approve_review"].func
        result = tool_func(
            task_id=sample_task_needs_review.id,
            stage_name="development",
            approval_notes="Looks good, all tests pass.",
        )

        assert "error" not in result
        mock_task_manager.approve_review.assert_called_once_with(
            sample_task_needs_review.id,
            "development",
            approval_notes="Looks good, all tests pass.",
            by_session_id=ANY,
        )

    def test_approve_task_not_found(self, stage_ops_registry, mock_task_manager) -> None:
        """Test approving a task that doesn't exist."""
        from unittest.mock import patch

        from gobby.storage.tasks import TaskNotFoundError

        with patch(
            "gobby.mcp_proxy.tools.tasks._stage_review.resolve_task_id_for_mcp",
            side_effect=TaskNotFoundError("Task #999 not found"),
        ):
            tool_func = stage_ops_registry._tools["approve_review"].func
            result = tool_func(
                task_id="#999",
                stage_name="development",
            )

        assert "error" in result

    def test_approve_releases_own_running_agent_dispatch_mutex(
        self, stage_ops_registry, mock_task_manager, sample_task_needs_review
    ) -> None:
        """A spawned reviewer may release its own dispatch lease before verdict."""
        from unittest.mock import patch

        mock_task_manager.get_task.return_value = sample_task_needs_review
        mock_task_manager.approve_review.return_value = _task(
            status="review_approved",
            description=sample_task_needs_review.description,
            seq_num=sample_task_needs_review.seq_num,
        )
        mock_task_manager.db.fetchone.return_value = {"id": "run-1"}
        mutexes = MagicMock()
        mutexes.get_mutex.return_value = SimpleNamespace(run_id="run-1")
        mutexes.clear_by_run_id.return_value = 1

        with patch(
            "gobby.mcp_proxy.tools.tasks._dispatch_mutex_release.TaskDispatchMutexManager",
            return_value=mutexes,
        ):
            tool_func = stage_ops_registry._tools["approve_review"].func
            result = tool_func(
                task_id=sample_task_needs_review.id,
                stage_name="development",
            )

        assert "error" not in result
        mock_task_manager.db.fetchone.assert_called_once()
        assert mock_task_manager.db.fetchone.call_args.args[1] == (
            "run-1",
            "resolved-session-abc",
            sample_task_needs_review.id,
        )
        mutexes.clear_by_run_id.assert_called_once_with("run-1")
        mock_task_manager.approve_review.assert_called_once()

    def test_approve_keeps_dispatch_mutex_for_other_agent(
        self, stage_ops_registry, mock_task_manager, sample_task_needs_review
    ) -> None:
        """Review verdicts do not clear another run's active dispatch lease."""
        from unittest.mock import patch

        mock_task_manager.get_task.return_value = sample_task_needs_review
        mock_task_manager.approve_review.return_value = _task(
            status="review_approved",
            description=sample_task_needs_review.description,
            seq_num=sample_task_needs_review.seq_num,
        )
        mock_task_manager.db.fetchone.return_value = None
        mutexes = MagicMock()
        mutexes.get_mutex.return_value = SimpleNamespace(run_id="run-1")

        with patch(
            "gobby.mcp_proxy.tools.tasks._dispatch_mutex_release.TaskDispatchMutexManager",
            return_value=mutexes,
        ):
            tool_func = stage_ops_registry._tools["approve_review"].func
            result = tool_func(
                task_id=sample_task_needs_review.id,
                stage_name="development",
            )

        assert "error" not in result
        mutexes.clear_by_run_id.assert_not_called()
        mock_task_manager.approve_review.assert_called_once()


class TestApproveSignoffSummary:
    """signoff_summary writes the adversary_verdict session variable."""

    @pytest.fixture(autouse=True)
    def _set_session_context(self):
        with session_context_for_test("session-abc"):
            yield

    @pytest.fixture
    def registry_with_ctx(self, mock_task_manager):
        from gobby.mcp_proxy.tools.tasks._context import RegistryContext
        from gobby.mcp_proxy.tools.tasks._stage_ops import create_stage_ops_registry

        ctx = RegistryContext(
            task_manager=mock_task_manager,
        )
        ctx.resolve_session_id = MagicMock(return_value="resolved-session-abc")
        ctx.session_task_manager = MagicMock()
        ctx.session_var_manager = MagicMock()
        ctx.session_manager = MagicMock()
        ctx.session_manager.get.return_value = None
        ctx.get_project_repo_path = MagicMock(return_value=None)
        return create_stage_ops_registry(ctx), ctx

    def test_explicit_signoff_summary_is_persisted(
        self, registry_with_ctx, mock_task_manager, sample_task_needs_review
    ) -> None:
        registry, ctx = registry_with_ctx
        mock_task_manager.get_task.return_value = sample_task_needs_review
        mock_task_manager.approve_review.return_value = _task(
            status="review_approved",
            description=sample_task_needs_review.description,
            seq_num=sample_task_needs_review.seq_num,
        )

        tool_func = registry._tools["approve_review"].func
        result = tool_func(
            task_id=sample_task_needs_review.id,
            stage_name="development",
            signoff_summary="APPROVED: round 13, no blocking findings",
        )

        assert "error" not in result
        ctx.session_var_manager.set_variable.assert_called_once_with(
            "resolved-session-abc",
            "adversary_verdict",
            "APPROVED: round 13, no blocking findings",
        )

    def test_omitted_signoff_summary_synthesizes_stock_template(
        self, registry_with_ctx, mock_task_manager, sample_task_needs_review
    ) -> None:
        registry, ctx = registry_with_ctx
        mock_task_manager.get_task.return_value = sample_task_needs_review
        mock_task_manager.approve_review.return_value = _task(
            status="review_approved",
            description=sample_task_needs_review.description,
            seq_num=sample_task_needs_review.seq_num,
        )

        tool_func = registry._tools["approve_review"].func
        result = tool_func(task_id=sample_task_needs_review.id, stage_name="development")

        assert "error" not in result
        ctx.session_var_manager.set_variable.assert_called_once_with(
            "resolved-session-abc",
            "adversary_verdict",
            "Approved #42",
        )
