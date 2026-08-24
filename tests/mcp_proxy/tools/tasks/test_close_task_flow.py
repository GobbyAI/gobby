"""Checklist evaluation and conditional-close flow contracts."""

from __future__ import annotations

import threading
from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import gobby.mcp_proxy.tools.tasks._lifecycle_close as lifecycle
import gobby.mcp_proxy.tools.tasks._lifecycle_close_finalization as close_finalization
from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.mcp_proxy.tools.tasks._close_evaluation_support import (
    CloseAttributionSnapshot,
    CloseEvaluationFingerprint,
)
from gobby.mcp_proxy.tools.tasks._context import RegistryContext
from gobby.mcp_proxy.tools.tasks._lifecycle_close import (
    _commit_close,
    _evaluate_close,
    register_close_task,
)
from gobby.mcp_proxy.tools.tasks._lifecycle_close_preview import CloseEvaluation
from gobby.mcp_proxy.tools.tasks._lifecycle_validation import ValidationResult
from gobby.mcp_proxy.tools.tasks._task_scope import TaskScopeEvaluation
from gobby.storage.tasks import Task
from gobby.tasks.transcript_evidence import TranscriptEvidence, TranscriptValidationRun
from gobby.workflows.state_manager import SessionVariableManager

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


def _ready_evaluation(
    task: Task,
    *,
    attribution: CloseAttributionSnapshot | None = None,
    children_state: tuple[tuple[str, str | None, bool], ...] = (),
) -> CloseEvaluation:
    if attribution is None:
        attribution = CloseAttributionSnapshot(
            owner_session_id=task.claimed_by_session_id or "",
            attributed=False,
            raw_paths=frozenset(),
            edited_paths=frozenset(),
            had_attributed_edits=False,
            claim_started_at=None,
        )
    evaluation = CloseEvaluation(task.id)
    evaluation.task = task
    evaluation.task_id = task.id
    evaluation.repo_path = "/repo"
    evaluation.resolved_session_id = task.claimed_by_session_id
    evaluation.fingerprint = CloseEvaluationFingerprint.capture(
        task,
        children_state=children_state,
        attribution=attribution,
    )
    evaluation.scope_snapshot = ((), (), ())
    evaluation.pass_gate(11, "criteria_review", "Passed.")
    return evaluation


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
async def test_empty_task_edit_entry_allows_no_edit_research_close() -> None:
    task = replace(_task(), category="research")
    ctx = _ctx(task, validator=object())
    ctx.session_var_manager = cast(
        SessionVariableManager,
        SimpleNamespace(get_variables=lambda _session_id: {"task_edited_files": {task.id: []}}),
    )
    review = AsyncMock(
        return_value=ValidationResult(
            can_close=True,
            validation_status="valid",
            validation_feedback="Research criteria satisfied.",
            reset_reason="llm_valid",
        )
    )

    with (
        patch.object(lifecycle, "resolve_task_id_for_mcp", return_value=task.id),
        patch.object(lifecycle, "resolve_task_repo_path", return_value="/repo"),
        patch.object(close_finalization, "_claimed_session_window_start", return_value=None),
        patch.object(lifecycle, "resolve_close_commit_shas", return_value=([], None)),
        patch.object(lifecycle, "collect_commit_diff_text", return_value=""),
        patch.object(lifecycle, "evaluate_criteria_review", review),
    ):
        evaluation = await _evaluate_close(
            ctx,
            task_id=task.id,
            reason="completed",
            changes_summary="Completed read-only research.",
            commit_sha=None,
            project_path=None,
            response_detail="diagnostic",
        )

    assert evaluation.ready is True
    assert evaluation.had_attributed_edits is False
    assert evaluation.commit_shas == []
    review.assert_awaited_once()


@pytest.mark.asyncio
async def test_no_work_disposition_skips_delivery_gates_but_runs_review() -> None:
    task = replace(_task(), category="research")
    ctx = _ctx(task, validator=object())
    ctx.session_var_manager = cast(
        SessionVariableManager,
        SimpleNamespace(get_variables=lambda _session_id: {"task_edited_files": {task.id: []}}),
    )
    review = AsyncMock(
        return_value=ValidationResult(
            can_close=True,
            validation_status="valid",
            validation_feedback="Duplicate disposition is specific.",
            reset_reason="llm_valid",
        )
    )

    with (
        patch.object(lifecycle, "resolve_task_id_for_mcp", return_value=task.id),
        patch.object(lifecycle, "resolve_task_repo_path", return_value="/repo"),
        patch.object(close_finalization, "_claimed_session_window_start", return_value=None),
        patch.object(lifecycle, "resolve_close_commit_shas", return_value=([], None)),
        patch.object(lifecycle, "collect_commit_diff_text", return_value=""),
        patch.object(lifecycle, "evaluate_criteria_review", review),
    ):
        evaluation = await _evaluate_close(
            ctx,
            task_id=task.id,
            reason="duplicate",
            changes_summary="Duplicate of #100, which owns this exact behavior.",
            commit_sha=None,
            project_path=None,
            response_detail="diagnostic",
        )

    assert evaluation.ready is True
    delivery = {gate.name: gate.status for gate in evaluation.gates[10:13]}
    assert delivery == {
        "acceptance_artifacts": "skipped",
        "tdd_evidence": "skipped",
        "epic_guards": "skipped",
    }
    review.assert_awaited_once()


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
        patch.object(close_finalization, "_claimed_session_window_start", return_value=None),
        patch.object(close_finalization, "_committable_task_paths", return_value={"src/a.py"}),
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
    assert [gate.item for gate in evaluation.gates] == list(range(1, 15))
    review.assert_awaited_once()


@pytest.mark.asyncio
async def test_scope_mismatch_stops_before_dirty_and_validation_gates() -> None:
    task = _task()
    ctx = _ctx(task, validator=object())
    scope = TaskScopeEvaluation(
        declared_paths=("tests/",),
        actual_paths=("src/gobby/service.py",),
        out_of_scope_paths=("src/gobby/service.py",),
        justification_error="A scope_justification is required for out-of-scope paths.",
    )
    transcript = AsyncMock()
    review = AsyncMock()

    with (
        patch.object(lifecycle, "resolve_task_id_for_mcp", return_value=task.id),
        patch.object(lifecycle, "resolve_task_repo_path", return_value="/repo"),
        patch.object(close_finalization, "_claimed_session_window_start", return_value=None),
        patch.object(
            close_finalization,
            "_committable_task_paths",
            return_value={"src/gobby/service.py"},
        ),
        patch.object(lifecycle, "resolve_close_commit_shas", return_value=(["abc123"], None)),
        patch.object(
            lifecycle,
            "validate_commit_requirements",
            return_value=ValidationResult(can_close=True),
        ),
        patch.object(lifecycle, "evaluate_task_scope", return_value=scope),
        patch.object(lifecycle, "_derive_close_transcript_evidence", transcript),
        patch.object(lifecycle, "evaluate_criteria_review", review),
        patch(
            "gobby.workflows.task_claim_state.target_task_has_edits",
            return_value=True,
        ),
        patch(
            "gobby.workflows.task_claim_state.task_edited_file_set",
            return_value={"src/gobby/service.py"},
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

    assert evaluation.error == "task_scope_mismatch"
    assert evaluation.gates[-1].item == 8
    assert evaluation.extra["out_of_scope_paths"] == ["src/gobby/service.py"]
    transcript.assert_not_awaited()
    review.assert_not_awaited()


@pytest.mark.asyncio
async def test_blocked_preview_returns_diagnostics_without_commit() -> None:
    ctx = _ctx(_task())
    evaluation = CloseEvaluation("task", response_detail="diagnostic").fail(
        9,
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
        patch.object(lifecycle, "active_review_response", return_value=None),
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
    assert result["checklist"][0]["item"] == 9
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
    launch = AsyncMock()
    registry = InternalToolRegistry("gobby-tasks")
    register_close_task(registry, ctx)

    with (
        patch.object(lifecycle, "_evaluate_close", evaluate),
        patch.object(lifecycle, "_commit_close", commit),
        patch.object(lifecycle, "active_review_response", return_value=None),
        patch.object(lifecycle, "launch_close_review", launch),
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
    launch.assert_not_awaited()
    evaluate.assert_awaited_once()
    commit.assert_awaited_once()
    awaited = commit.await_args
    assert awaited is not None
    assert awaited.args[1] is evaluation


def test_close_task_schema_has_automated_review_surface() -> None:
    registry = InternalToolRegistry("gobby-tasks")
    register_close_task(registry, _ctx(_task()))

    close_schema = registry.get_schema("close_task")
    submit_schema = registry.get_schema("submit_close_review")

    assert close_schema is not None
    assert "review_run_id" not in close_schema["inputSchema"]["properties"]
    assert submit_schema is not None
    assert submit_schema["inputSchema"]["required"] == ["review_id", "verdict"]


@pytest.mark.asyncio
async def test_commit_set_change_returns_stale_without_close() -> None:
    task = _task()
    ctx = _ctx(task)
    evaluation = CloseEvaluation(task.id)
    evaluation.task = task
    evaluation.task_id = task.id
    evaluation.repo_path = "/repo"
    evaluation.commit_shas = ["before"]
    evaluation.pass_gate(11, "criteria_review", "Passed.")

    with patch.object(
        close_finalization,
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


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    [
        "category",
        "task-type",
        "claim-owner",
        "validation-criteria",
        "escalation",
        "closure",
        "parent-linkage",
    ],
)
async def test_gate_input_change_returns_stale_without_close(change: str) -> None:
    task = _task()
    ctx = _ctx(task)
    changed_task = {
        "category": lambda: replace(task, category="docs"),
        "task-type": lambda: replace(task, task_type="epic"),
        "claim-owner": lambda: replace(
            task,
            claimed_by_session_id="00000000-0000-4000-8000-000000000999",
        ),
        "validation-criteria": lambda: replace(
            task,
            validation_criteria="Changed criteria.",
        ),
        "escalation": lambda: replace(task, is_escalated=True),
        "closure": lambda: replace(
            task,
            closed_at=datetime(2026, 7, 27, 12, 1, tzinfo=UTC),
        ),
        "parent-linkage": lambda: replace(
            task,
            parent_task_id="00000000-0000-4000-8000-000000000888",
        ),
    }[change]()
    cast(MagicMock, ctx.task_manager.get_task).return_value = changed_task
    evaluation = _ready_evaluation(task)

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


@pytest.mark.asyncio
async def test_children_change_returns_stale_without_close() -> None:
    task = _task()
    child = replace(
        _task(),
        id="00000000-0000-4000-8000-000000000102",
        parent_task_id=task.id,
        closed_at=datetime(2026, 7, 27, 12, 1, tzinfo=UTC),
    )
    ctx = _ctx(task)
    cast(MagicMock, ctx.task_manager.list_tasks).return_value = [child]
    evaluation = _ready_evaluation(task)

    result = await _commit_close(
        ctx,
        evaluation,
        reason="completed",
        skip_validation=False,
        override_justification=None,
        commit_sha=None,
    )

    assert result["error"] == "stale_task_state"
    cast(MagicMock, ctx.task_manager.close_task).assert_not_called()


@pytest.mark.asyncio
async def test_new_attributed_paths_during_review_return_stale() -> None:
    task = _task()
    ctx = _ctx(task)
    evaluation = _ready_evaluation(task)

    with (
        patch(
            "gobby.workflows.task_claim_state.target_task_has_edits",
            return_value=True,
        ),
        patch(
            "gobby.workflows.task_claim_state.task_edited_file_set",
            return_value={"src/new.py"},
        ),
        patch.object(
            close_finalization,
            "_committable_task_paths",
            return_value={"src/new.py"},
        ),
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
    cast(MagicMock, ctx.task_manager.close_task).assert_not_called()


@pytest.mark.asyncio
async def test_same_owner_reclaim_window_change_returns_stale() -> None:
    task = _task()
    ctx = _ctx(task)
    attribution = CloseAttributionSnapshot(
        owner_session_id=task.claimed_by_session_id or "",
        attributed=False,
        raw_paths=frozenset(),
        edited_paths=frozenset(),
        had_attributed_edits=False,
        claim_started_at="2026-07-27T12:00:00Z",
    )
    evaluation = _ready_evaluation(task, attribution=attribution)

    with patch.object(
        close_finalization,
        "_claimed_session_window_start",
        return_value="2026-07-27T12:05:00Z",
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
    cast(MagicMock, ctx.task_manager.close_task).assert_not_called()


@pytest.mark.asyncio
async def test_benign_bookkeeping_change_does_not_stale_close() -> None:
    task = _task()
    fresh = replace(
        task,
        updated_at=datetime(2026, 7, 27, 12, 1, tzinfo=UTC),
        path_cache="19236.19238",
    )
    ctx = _ctx(task)
    cast(MagicMock, ctx.task_manager.get_task).return_value = fresh
    evaluation = _ready_evaluation(task)

    with (
        patch.object(
            close_finalization,
            "resolve_close_commit_shas",
            return_value=([], None),
        ) as resolve_commits,
        patch.object(
            close_finalization,
            "link_close_commit_shas",
            return_value=(fresh, None),
        ) as link_commits,
        patch.object(
            close_finalization,
            "notify_parent_on_task_state_change",
        ) as notify_parent,
    ):
        result = await _commit_close(
            ctx,
            evaluation,
            reason="completed",
            skip_validation=False,
            override_justification=None,
            commit_sha=None,
        )

    assert result["closed"] is True
    cast(MagicMock, ctx.task_manager.close_task).assert_called_once()
    resolve_commits.assert_called_once()
    link_commits.assert_called_once()
    notify_parent.assert_called_once()


@pytest.mark.asyncio
async def test_scope_justification_is_rechecked_and_persisted_on_close() -> None:
    task = _task()
    ctx = _ctx(task)
    evaluation = _ready_evaluation(task)
    justification = "The shared implementation path is required by the scoped tests."
    scope = TaskScopeEvaluation(
        declared_paths=("tests/",),
        actual_paths=("src/gobby/service.py",),
        out_of_scope_paths=("src/gobby/service.py",),
        scope_justification=justification,
    )
    evaluation.scope_snapshot = scope.snapshot()
    evaluation.scope_justification = justification

    with (
        patch.object(close_finalization, "resolve_close_commit_shas", return_value=([], None)),
        patch.object(close_finalization, "evaluate_task_scope", return_value=scope),
        patch.object(close_finalization, "link_close_commit_shas", return_value=(task, None)),
        patch.object(close_finalization, "notify_parent_on_task_state_change"),
    ):
        result = await _commit_close(
            ctx,
            evaluation,
            reason="completed",
            skip_validation=False,
            override_justification=None,
            commit_sha=None,
        )

    assert result["closed"] is True
    close_task = cast(MagicMock, ctx.task_manager.close_task)
    assert close_task.call_count == 1
    assert close_task.call_args.args == (task.id,)
    assert close_task.call_args.kwargs["validation_override_reason"] == (
        f"Task scope justification: {justification}"
    )
    assert close_task.call_args.kwargs["reason"] == "completed"


@pytest.mark.asyncio
async def test_justified_escalated_close_skips_review_and_persists_override() -> None:
    task = replace(
        _task(),
        is_escalated=True,
        escalated_at=datetime(2026, 7, 27, 12, 1, tzinfo=UTC),
        escalation_reason="Repeated invalid review",
        validation_fail_count=5,
    )
    ctx = _ctx(task, validator=object())
    review = AsyncMock()

    with (
        patch.object(lifecycle, "resolve_task_id_for_mcp", return_value=task.id),
        patch.object(lifecycle, "resolve_task_repo_path", return_value="/repo"),
        patch.object(close_finalization, "_claimed_session_window_start", return_value=None),
        patch.object(lifecycle, "resolve_close_commit_shas", return_value=([], None)),
        patch.object(lifecycle, "evaluate_criteria_review", review),
    ):
        evaluation = await _evaluate_close(
            ctx,
            task_id=task.id,
            reason="completed",
            changes_summary="Deliberately resolved the escalated task.",
            commit_sha=None,
            project_path=None,
            response_detail="diagnostic",
            override_justification="Reviewed and accepted the current implementation.",
        )

    assert evaluation.ready is True
    assert evaluation.validation_reset_reason == "escalated_deliberate_close"
    assert evaluation.gates[-1].status == "skipped"
    review.assert_not_awaited()

    with (
        patch.object(close_finalization, "resolve_close_commit_shas", return_value=([], None)),
        patch.object(close_finalization, "link_close_commit_shas", return_value=(task, None)),
        patch.object(close_finalization, "_claimed_session_window_start", return_value=None),
        patch.object(close_finalization, "notify_parent_on_task_state_change"),
        patch.object(close_finalization, "_cleanup_closed_claim"),
    ):
        result = await _commit_close(
            ctx,
            evaluation,
            reason="completed",
            skip_validation=False,
            override_justification="Reviewed and accepted the current implementation.",
            commit_sha=None,
        )

    assert result["closed"] is True
    cast(MagicMock, ctx.task_manager.close_task).assert_called_once_with(
        task.id,
        reason="completed",
        closed_in_session_id=task.claimed_by_session_id,
        closed_commit_sha=None,
        closed_ancestors=[],
        validation_override_reason="Reviewed and accepted the current implementation.",
        expected_updated_at=task.updated_at,
        reset_validation_fail_count=True,
        validation_status="valid",
        validation_feedback=None,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("override_justification", [None, "   "])
async def test_escalated_close_without_justification_converges_on_actionable_blocker(
    override_justification: str | None,
) -> None:
    task = replace(
        _task(),
        is_escalated=True,
        escalated_at=datetime(2026, 7, 27, 12, 1, tzinfo=UTC),
        escalation_reason="Repeated invalid review",
        validation_fail_count=5,
    )
    ctx = _ctx(task, validator=object())
    review = AsyncMock()

    with (
        patch.object(lifecycle, "resolve_task_id_for_mcp", return_value=task.id),
        patch.object(lifecycle, "resolve_task_repo_path", return_value="/repo"),
        patch.object(close_finalization, "_claimed_session_window_start", return_value=None),
        patch.object(lifecycle, "resolve_close_commit_shas", return_value=([], None)),
        patch.object(lifecycle, "evaluate_criteria_review", review),
    ):
        results = [
            await _evaluate_close(
                ctx,
                task_id=task.id,
                reason="completed",
                changes_summary="Attempted deliberate close.",
                commit_sha=None,
                project_path=None,
                response_detail="diagnostic",
                override_justification=override_justification,
            )
            for _ in range(2)
        ]

    assert [evaluation.error for evaluation in results] == [
        "task_escalated",
        "task_escalated",
    ]
    assert all("override_justification" in (evaluation.action or "") for evaluation in results)
    review.assert_not_awaited()
    cast(MagicMock, ctx.task_manager.close_task).assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("override_justification", [None, "   "])
async def test_escalated_structural_parent_requires_justification(
    override_justification: str | None,
) -> None:
    task = replace(
        _task(),
        task_type="epic",
        is_escalated=True,
        escalated_at=datetime(2026, 7, 27, 12, 1, tzinfo=UTC),
        escalation_reason="Needs human review",
    )
    ctx = _ctx(task, validator=object())
    review = AsyncMock()

    with (
        patch.object(lifecycle, "resolve_task_id_for_mcp", return_value=task.id),
        patch.object(lifecycle, "resolve_task_repo_path", return_value="/repo"),
        patch.object(lifecycle, "resolve_close_commit_shas", return_value=([], None)),
        patch.object(lifecycle, "evaluate_criteria_review", review),
    ):
        evaluation = await _evaluate_close(
            ctx,
            task_id=task.id,
            reason="completed",
            changes_summary=None,
            commit_sha=None,
            project_path=None,
            response_detail="diagnostic",
            override_justification=override_justification,
        )

    assert evaluation.error == "task_escalated"
    assert "override_justification" in (evaluation.action or "")
    review.assert_not_awaited()
    cast(MagicMock, ctx.task_manager.close_task).assert_not_called()


@pytest.mark.asyncio
async def test_justified_escalated_structural_parent_closes_and_persists_override() -> None:
    task = replace(
        _task(),
        task_type="epic",
        is_escalated=True,
        escalated_at=datetime(2026, 7, 27, 12, 1, tzinfo=UTC),
        escalation_reason="Needs human review",
        validation_fail_count=5,
    )
    ctx = _ctx(task, validator=object())
    review = AsyncMock()

    with (
        patch.object(lifecycle, "resolve_task_id_for_mcp", return_value=task.id),
        patch.object(lifecycle, "resolve_task_repo_path", return_value="/repo"),
        patch.object(lifecycle, "resolve_close_commit_shas", return_value=([], None)),
        patch.object(lifecycle, "evaluate_criteria_review", review),
    ):
        evaluation = await _evaluate_close(
            ctx,
            task_id=task.id,
            reason="completed",
            changes_summary=None,
            commit_sha=None,
            project_path=None,
            response_detail="diagnostic",
            override_justification="Reviewed: obsolete escalation, closing deliberately.",
        )

    assert evaluation.ready is True
    assert evaluation.validation_reset_reason == "escalated_deliberate_close"
    assert evaluation.gates[-1].status == "skipped"
    review.assert_not_awaited()

    with (
        patch.object(close_finalization, "resolve_close_commit_shas", return_value=([], None)),
        patch.object(close_finalization, "link_close_commit_shas", return_value=(task, None)),
        patch.object(close_finalization, "notify_parent_on_task_state_change"),
        patch.object(close_finalization, "_cleanup_closed_claim"),
        patch("gobby.hooks.event_handlers._plan.on_epic_terminal"),
    ):
        result = await _commit_close(
            ctx,
            evaluation,
            reason="completed",
            skip_validation=False,
            override_justification="Reviewed: obsolete escalation, closing deliberately.",
            commit_sha=None,
        )

    assert result["closed"] is True
    cast(MagicMock, ctx.task_manager.close_task).assert_called_once_with(
        task.id,
        reason="completed",
        closed_in_session_id=task.claimed_by_session_id,
        closed_commit_sha=None,
        closed_ancestors=[],
        validation_override_reason="Reviewed: obsolete escalation, closing deliberately.",
        expected_updated_at=task.updated_at,
        reset_validation_fail_count=True,
        validation_status="valid",
        validation_feedback=None,
    )


@pytest.mark.asyncio
async def test_dirty_attributed_edit_stops_before_transcript_and_llm() -> None:
    task = _task()
    ctx = _ctx(task, validator=object())
    transcript = AsyncMock()
    review = AsyncMock()

    with (
        patch.object(lifecycle, "resolve_task_id_for_mcp", return_value=task.id),
        patch.object(lifecycle, "resolve_task_repo_path", return_value="/repo"),
        patch.object(close_finalization, "_claimed_session_window_start", return_value=None),
        patch.object(close_finalization, "_committable_task_paths", return_value={"src/a.py"}),
        patch.object(lifecycle, "_has_committable_edits", return_value=True),
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
        patch.object(lifecycle, "_derive_close_transcript_evidence", transcript),
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

    assert evaluation.error == "uncommitted_task_edits"
    assert evaluation.gates[-1].item == 9
    transcript.assert_not_awaited()
    review.assert_not_awaited()


@pytest.mark.asyncio
async def test_epic_skips_leaf_gates_without_llm() -> None:
    task = replace(_task(), task_type="epic", commits=["abc123"])
    ctx = _ctx(task, validator=object())
    review = AsyncMock()

    with (
        patch.object(lifecycle, "resolve_task_id_for_mcp", return_value=task.id),
        patch.object(lifecycle, "resolve_task_repo_path", return_value="/repo"),
        patch.object(lifecycle, "evaluate_criteria_review", review),
    ):
        evaluation = await _evaluate_close(
            ctx,
            task_id=task.id,
            reason="completed",
            changes_summary=None,
            commit_sha=None,
            project_path=None,
            response_detail="diagnostic",
        )

    assert evaluation.ready is True
    assert evaluation.commit_shas == ["abc123"]
    assert [gate.item for gate in evaluation.gates] == list(range(1, 15))
    assert all(gate.status == "skipped" for gate in evaluation.gates[4:])
    review.assert_not_awaited()


@pytest.mark.asyncio
async def test_commit_epic_persists_allowed_valid_status() -> None:
    task = replace(_task(), task_type="epic")
    ctx = _ctx(task)
    manager = cast(MagicMock, ctx.task_manager)
    evaluation = CloseEvaluation(task.id)
    evaluation.task = task
    evaluation.task_id = task.id
    evaluation.repo_path = "/repo"
    evaluation.resolved_session_id = task.claimed_by_session_id
    evaluation.commit_shas = []
    evaluation.is_epic = True
    evaluation.skip_leaf_checks = True
    evaluation.fingerprint = CloseEvaluationFingerprint.capture(
        task,
        children_state=(),
        attribution=None,
    )

    with (
        patch.object(close_finalization, "resolve_close_commit_shas", return_value=([], None)),
        patch.object(close_finalization, "link_close_commit_shas", return_value=(task, None)),
        patch.object(close_finalization, "notify_parent_on_task_state_change"),
        patch.object(close_finalization, "_cleanup_closed_claim"),
        patch("gobby.hooks.event_handlers._plan.on_epic_terminal"),
    ):
        result = await _commit_close(
            ctx,
            evaluation,
            reason="completed",
            skip_validation=False,
            override_justification=None,
            commit_sha=None,
        )

    assert result["closed"] is True
    assert manager.close_task.call_args.kwargs["validation_status"] == "valid"


def test_closed_task_cleanup_removes_only_its_edit_entry() -> None:
    variables = {
        "claimed_tasks": {"task-1": "#1", "task-2": "#2"},
        "active_task_id": "task-1",
        "task_edited_files": {
            "task-1": ["src/closed.py"],
            "task-2": ["src/remaining.py"],
        },
    }
    session_var_manager = MagicMock()
    session_var_manager.get_variables.return_value = variables
    session_manager = MagicMock()
    ctx = cast(
        RegistryContext,
        SimpleNamespace(
            session_task_manager=MagicMock(),
            session_var_manager=session_var_manager,
            session_manager=session_manager,
        ),
    )
    evaluation = CloseEvaluation("task-1")
    evaluation.task_id = "task-1"
    evaluation.resolved_session_id = "session"
    evaluation.edit_session_id = "session"

    close_finalization._cleanup_closed_claim(ctx, evaluation, ["abc123"])

    updates = session_var_manager.merge_variables.call_args.args[1]
    assert updates["task_edited_files"] == {"task-2": ["src/remaining.py"]}
    session_manager.clear_had_edits.assert_not_called()


@pytest.mark.asyncio
async def test_close_resolves_commits_and_validates_them_off_the_event_loop() -> None:
    """Both helpers shell out to git, so neither may run on the loop.

    resolve_close_commit_shas and validate_commit_requirements both reach
    normalize_commit_sha -> run_git_command -> subprocess.run, which forks git
    and then blocks waiting for it, up to a 5s timeout. The loop-lag watchdog
    caught all three entry points doing it on the loop thread (#20861).
    """
    task = _task()
    ctx = _ctx(task, validator=object())
    # Both helpers run before the scope gate, so a scope mismatch stops the
    # evaluation right after them and keeps this test off the later gates.
    scope = TaskScopeEvaluation(
        declared_paths=("tests/",),
        actual_paths=("src/gobby/service.py",),
        out_of_scope_paths=("src/gobby/service.py",),
        justification_error="A scope_justification is required for out-of-scope paths.",
    )
    resolved_on: list[int] = []
    validated_on: list[int] = []

    def record_resolve(*_args: object, **_kwargs: object) -> tuple[list[str], None]:
        resolved_on.append(threading.get_ident())
        return (["abc123"], None)

    def record_validate(*_args: object, **_kwargs: object) -> ValidationResult:
        validated_on.append(threading.get_ident())
        return ValidationResult(can_close=True)

    with (
        patch.object(lifecycle, "resolve_task_id_for_mcp", return_value=task.id),
        patch.object(lifecycle, "resolve_task_repo_path", return_value="/repo"),
        patch.object(close_finalization, "_claimed_session_window_start", return_value=None),
        patch.object(
            close_finalization,
            "_committable_task_paths",
            return_value={"src/gobby/service.py"},
        ),
        patch.object(lifecycle, "resolve_close_commit_shas", record_resolve),
        patch.object(lifecycle, "validate_commit_requirements", record_validate),
        patch.object(lifecycle, "evaluate_task_scope", return_value=scope),
        patch.object(lifecycle, "_derive_close_transcript_evidence", AsyncMock()),
        patch.object(lifecycle, "evaluate_criteria_review", AsyncMock()),
        patch(
            "gobby.workflows.task_claim_state.target_task_has_edits",
            return_value=True,
        ),
        patch(
            "gobby.workflows.task_claim_state.task_edited_file_set",
            return_value={"src/gobby/service.py"},
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

    assert evaluation.error == "task_scope_mismatch"
    loop_thread = threading.get_ident()
    assert resolved_on, "resolve_close_commit_shas must run"
    assert validated_on, "validate_commit_requirements must run"
    assert loop_thread not in resolved_on
    assert loop_thread not in validated_on


@pytest.mark.asyncio
async def test_commit_close_links_and_closes_off_the_event_loop() -> None:
    """The two mutations commit_close makes both block, so neither may run on the loop.

    link_close_commit_shas reaches git by its own route -- the storage layer's
    link_commit -> normalize_commit_sha -> run_git_command -> subprocess.run --
    which #20861's three offloads did not cover. The storage close_task
    transition runs synchronous psycopg, and _close_eligible_ancestors walks up
    the tree inside the transaction it holds open, so its cost grows with depth
    and sibling count. The loop-lag watchdog caught both below commit_close
    (#20862).
    """
    task = _task()
    ctx = _ctx(task)
    evaluation = _ready_evaluation(task)
    linked_on: list[int] = []
    closed_on: list[int] = []

    def record_link(*_args: object, **_kwargs: object) -> tuple[Task, None]:
        linked_on.append(threading.get_ident())
        return (task, None)

    def record_close(*_args: object, **_kwargs: object) -> None:
        closed_on.append(threading.get_ident())

    cast(MagicMock, ctx.task_manager.close_task).side_effect = record_close

    with (
        patch.object(close_finalization, "resolve_close_commit_shas", return_value=([], None)),
        patch.object(close_finalization, "link_close_commit_shas", record_link),
        patch.object(close_finalization, "notify_parent_on_task_state_change"),
    ):
        result = await _commit_close(
            ctx,
            evaluation,
            reason="completed",
            skip_validation=False,
            override_justification=None,
            commit_sha=None,
        )

    assert result["closed"] is True
    loop_thread = threading.get_ident()
    assert linked_on, "link_close_commit_shas must run"
    assert closed_on, "the storage close_task transition must run"
    assert loop_thread not in linked_on
    assert loop_thread not in closed_on
