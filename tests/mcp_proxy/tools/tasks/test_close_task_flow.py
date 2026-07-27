"""Checklist evaluation and conditional-close flow contracts."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import gobby.mcp_proxy.tools.tasks._lifecycle_close as lifecycle
from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.mcp_proxy.tools.tasks._context import RegistryContext
from gobby.mcp_proxy.tools.tasks._lifecycle_close import (
    _commit_close,
    _evaluate_close,
    register_close_task,
)
from gobby.mcp_proxy.tools.tasks._lifecycle_close_preview import CloseEvaluation
from gobby.mcp_proxy.tools.tasks._lifecycle_validation import ValidationResult
from gobby.storage.tasks import Task
from gobby.tasks.transcript_evidence import TranscriptEvidence, TranscriptValidationRun

pytestmark = pytest.mark.unit


def _task(*, criteria: str | None = "Focused tests pass.") -> Task:
    return Task(
        id="00000000-0000-4000-8000-000000000101",
        project_id="00000000-0000-4000-8000-000000000201",
        title="Close checklist leaf",
        category="code",
        priority=2,
        task_type="task",
        created_at=datetime(2026, 7, 27, 12, tzinfo=UTC),
        updated_at=datetime(2026, 7, 27, 12, tzinfo=UTC),
        claimed_by_session_id="00000000-0000-4000-8000-000000000301",
        validation_criteria=criteria,
        validation_fail_count=0,
        stages=({"stage_name": "development", "position": 0, "state": "in_progress"},),
    )


def _ctx(task: Task, validator: object = None) -> RegistryContext:
    manager = MagicMock()
    manager.db = MagicMock()
    manager.get_task.return_value = task
    manager.list_tasks.return_value = []
    return cast(
        RegistryContext,
        SimpleNamespace(
            task_manager=manager,
            task_validator=validator,
            project_manager=MagicMock(),
            session_var_manager=SimpleNamespace(get_variables=lambda _session_id: {}),
            validation_config=None,
            resolve_session_id=lambda session_id: session_id,
            get_current_project_name=lambda: "gobby",
        ),
    )


@pytest.mark.asyncio
async def test_missing_criteria_stops_before_llm() -> None:
    task = _task(criteria=None)
    review = AsyncMock()
    ctx = _ctx(task, validator=object())

    with (
        patch.object(lifecycle, "resolve_task_id_for_mcp", return_value=task.id),
        patch.object(lifecycle, "resolve_task_repo_path", return_value="/repo"),
        patch.object(lifecycle, "evaluate_criteria_review", review),
    ):
        evaluation = await _evaluate_close(
            ctx,
            task_id=task.id,
            reason="completed",
            changes_summary="Implemented.",
            commit_sha=None,
            project_path=None,
            response_detail="diagnostic",
        )

    assert evaluation.error == "missing_validation_criteria"
    assert [gate.item for gate in evaluation.gates] == [1, 2, 3, 4, 5]
    review.assert_not_awaited()


@pytest.mark.asyncio
async def test_ready_leaf_runs_criteria_review_exactly_once() -> None:
    task = _task()
    ctx = _ctx(task, validator=object())
    review = AsyncMock(
        return_value=ValidationResult(
            can_close=True,
            validation_status="valid",
            validation_feedback="Criteria satisfied.",
            reset_reason="llm_valid",
            extra={"verdict": {"status": "valid"}},
        )
    )
    now = datetime(2026, 7, 27, 12, 5, tzinfo=UTC)
    transcript = TranscriptEvidence(
        validation_runs=(
            TranscriptValidationRun(
                session_id=task.claimed_by_session_id or "",
                source="codex",
                command="uv run pytest tests/tasks/test_close_checklist.py -q",
                categories=("test",),
                matcher_id="pytest",
                label="pytest",
                outcome="success",
                started_at=now,
                completed_at=now,
                order=1,
                exit_code=0,
            ),
        ),
        sessions=(task.claimed_by_session_id or "",),
    )

    with (
        patch.object(lifecycle, "resolve_task_id_for_mcp", return_value=task.id),
        patch.object(lifecycle, "resolve_task_repo_path", return_value="/repo"),
        patch.object(lifecycle, "_claimed_session_window_start", return_value=None),
        patch.object(lifecycle, "_committable_task_paths", return_value={"src/a.py"}),
        patch.object(lifecycle, "_has_committable_edits", return_value=False),
        patch.object(
            lifecycle,
            "resolve_close_commit_shas",
            return_value=(["abc123"], None),
        ),
        patch.object(
            lifecycle,
            "validate_commit_requirements",
            return_value=ValidationResult(can_close=True),
        ),
        patch.object(lifecycle, "active_validation_backoff", return_value=None),
        patch.object(
            lifecycle,
            "_derive_close_transcript_evidence",
            AsyncMock(return_value=transcript),
        ),
        patch.object(lifecycle, "collect_commit_diff_text", return_value="diff"),
        patch.object(lifecycle, "evaluate_criteria_review", review),
        patch(
            "gobby.workflows.task_claim_state.target_task_has_edits",
            return_value=True,
        ),
        patch(
            "gobby.workflows.task_claim_state.task_edited_file_set",
            return_value={"src/a.py"},
        ),
    ):
        evaluation = await _evaluate_close(
            ctx,
            task_id=task.id,
            reason="completed",
            changes_summary="Implemented and tested.",
            commit_sha="abc123",
            project_path=None,
            response_detail="diagnostic",
        )

    assert evaluation.ready is True
    assert [gate.item for gate in evaluation.gates] == list(range(1, 11))
    review.assert_awaited_once()


@pytest.mark.asyncio
async def test_blocked_preview_returns_diagnostics_without_commit() -> None:
    ctx = _ctx(_task())
    evaluation = CloseEvaluation("task", response_detail="diagnostic").fail(
        8,
        "uncommitted_task_edits",
        "uncommitted_task_edits",
        "Commit the task-attributed edits.",
    )
    evaluate = AsyncMock(return_value=evaluation)
    commit = AsyncMock()
    registry = InternalToolRegistry("gobby-tasks")
    register_close_task(registry, ctx)

    with (
        patch.object(lifecycle, "_evaluate_close", evaluate),
        patch.object(lifecycle, "_commit_close", commit),
    ):
        result = await registry.call(
            "close_task",
            {
                "task_id": "task",
                "changes_summary": "Implemented.",
                "preview": True,
                "response_detail": "diagnostic",
            },
        )

    assert result["success"] is False
    assert result["closed"] is False
    assert result["checklist"][0]["item"] == 8
    evaluate.assert_awaited_once()
    commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_ready_preview_commits_same_evaluation() -> None:
    ctx = _ctx(_task())
    evaluation = CloseEvaluation("task")
    evaluation.pass_gate(1, "task_exists", "Task exists.")
    evaluate = AsyncMock(return_value=evaluation)
    commit = AsyncMock(
        return_value={"success": True, "closed": True, "preview": False, "can_close": True}
    )
    registry = InternalToolRegistry("gobby-tasks")
    register_close_task(registry, ctx)

    with (
        patch.object(lifecycle, "_evaluate_close", evaluate),
        patch.object(lifecycle, "_commit_close", commit),
    ):
        result = await registry.call(
            "close_task",
            {
                "task_id": "task",
                "changes_summary": "Implemented.",
                "preview": True,
            },
        )

    assert result["closed"] is True
    assert result["preview"] is True
    evaluate.assert_awaited_once()
    commit.assert_awaited_once()
    awaited = commit.await_args
    assert awaited is not None
    assert awaited.args[1] is evaluation


@pytest.mark.asyncio
async def test_commit_set_change_returns_stale_without_close() -> None:
    task = _task()
    ctx = _ctx(task)
    evaluation = CloseEvaluation(task.id)
    evaluation.task = task
    evaluation.task_id = task.id
    evaluation.repo_path = "/repo"
    evaluation.commit_shas = ["before"]
    evaluation.pass_gate(10, "criteria_review", "Passed.")

    with patch.object(
        lifecycle,
        "resolve_close_commit_shas",
        return_value=(["after"], None),
    ):
        result = await _commit_close(
            ctx,
            evaluation,
            reason="completed",
            skip_validation=False,
            override_justification=None,
            commit_sha=None,
        )

    assert result["error"] == "stale_task_state"
    assert result["closed"] is False
    cast(MagicMock, ctx.task_manager.close_task).assert_not_called()
