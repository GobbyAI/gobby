"""Phase 2 red contracts for record_pr_verdict MCP tool."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import gobby.mcp_proxy.tools.tasks._stage_ops as stage_ops
from gobby.storage.tasks import LocalTaskManager
from tests.phase2_stage_contract_helpers import register_contract_tests
from tests.storage.tasks._stage_test_helpers import (
    create_task,
    initialize_manifest,
    set_stage_state,
    spec,
    stage_row,
    task_row,
)

pytestmark = pytest.mark.unit


def _context(temp_db, sample_project) -> SimpleNamespace:
    task = create_task(temp_db, sample_project, task_type="feature")
    stage_states = SimpleNamespace(
        approve_review=Mock(return_value=SimpleNamespace(stage_name="pr", state="review_approved")),
        reject_review=Mock(return_value=SimpleNamespace(stage_name="pr", state="ready")),
        get=Mock(return_value=SimpleNamespace(stage_name="pr", state="needs_review")),
        complete_stage=Mock(side_effect=AssertionError("record_pr_verdict must not advance")),
    )
    return SimpleNamespace(
        task_manager=SimpleNamespace(
            db=temp_db,
            stage_states=stage_states,
            get_task=Mock(return_value=SimpleNamespace(id=task.id)),
            escalate_task=Mock(return_value=SimpleNamespace(id=task.id)),
        ),
        task_id=task.id,
        resolve_session_id=lambda session_ref: session_ref,
    )


def _record_pr_verdict(ctx: SimpleNamespace):
    tool = stage_ops.create_stage_ops_registry(ctx).get_tool("record_pr_verdict")
    assert tool is not None
    return tool


def _patch_stage_view(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        stage_ops,
        "stage_state_operation_view",
        lambda stage: {"stage_name": stage.stage_name, "state": stage.state},
    )


def _real_context(temp_db) -> SimpleNamespace:
    return SimpleNamespace(
        task_manager=LocalTaskManager(temp_db),
        resolve_session_id=lambda session_ref: session_ref,
    )


def _pr_task_in_review(
    temp_db,
    sample_project,
    *,
    max_review_rounds: int | None = None,
    review_round_count: int = 0,
):
    stage_kwargs = {}
    if max_review_rounds is not None:
        stage_kwargs["max_review_rounds"] = max_review_rounds
    task = create_task(temp_db, sample_project, task_type="feature")
    initialize_manifest(temp_db, task.id, [spec("pr", 1, **stage_kwargs)])
    set_stage_state(
        temp_db,
        task.id,
        "pr",
        "needs_review",
        review_round_count=review_round_count,
    )
    return task


def test_approved_calls_approve_review_no_advance(
    monkeypatch: pytest.MonkeyPatch,
    temp_db,
    sample_project,
) -> None:
    _patch_stage_view(monkeypatch)
    ctx = _context(temp_db, sample_project)

    result = _record_pr_verdict(ctx)(
        task_id=ctx.task_id,
        verdict="approve",
        findings="looks good",
        report_ref="pr-review.md",
    )

    assert result["stage"] == {"stage_name": "pr", "state": "review_approved"}
    ctx.task_manager.stage_states.approve_review.assert_called_once_with(
        ctx.task_id,
        "pr",
        by_session_id=None,
        notes="looks good",
    )
    ctx.task_manager.stage_states.complete_stage.assert_not_called()


def test_approved_writes_artifacts(
    monkeypatch: pytest.MonkeyPatch,
    temp_db,
    sample_project,
) -> None:
    _patch_stage_view(monkeypatch)
    ctx = _context(temp_db, sample_project)

    _record_pr_verdict(ctx)(
        task_id=ctx.task_id,
        verdict="approve",
        findings="looks good",
        report_ref="pr-review.md",
    )

    row = temp_db.fetchone(
        """
        SELECT structured_pr_verdict, pr_report_ref
        FROM task_delivery_campaigns
        WHERE task_id = %s
        """,
        (ctx.task_id,),
    )
    assert row is not None
    payload = json.loads(row["structured_pr_verdict"])
    assert payload == {
        "verdict": "approve",
        "findings": "looks good",
        "report_ref": "pr-review.md",
    }
    assert row["pr_report_ref"] == "pr-review.md"


def _assert_reject_review_for_verdict(
    monkeypatch: pytest.MonkeyPatch,
    verdict: str,
    temp_db,
    sample_project,
) -> None:
    _patch_stage_view(monkeypatch)
    ctx = _context(temp_db, sample_project)

    result = _record_pr_verdict(ctx)(
        task_id=ctx.task_id,
        verdict=verdict,
        findings="missing test evidence",
    )

    assert result["stage"] == {"stage_name": "pr", "state": "ready"}
    ctx.task_manager.stage_states.reject_review.assert_called_once_with(
        ctx.task_id,
        "pr",
        reason="missing test evidence",
        by_session_id=None,
        notes="missing test evidence",
    )
    ctx.task_manager.stage_states.approve_review.assert_not_called()
    ctx.task_manager.stage_states.complete_stage.assert_not_called()


def test_rejected_calls_reject_review(
    monkeypatch: pytest.MonkeyPatch, temp_db, sample_project
) -> None:
    _assert_reject_review_for_verdict(monkeypatch, "request_changes", temp_db, sample_project)


def test_needs_discussion_escalates(
    monkeypatch: pytest.MonkeyPatch,
    temp_db,
    sample_project,
) -> None:
    _patch_stage_view(monkeypatch)
    ctx = _context(temp_db, sample_project)

    result = _record_pr_verdict(ctx)(
        task_id=ctx.task_id,
        verdict="needs_discussion",
        findings="human decision required",
    )

    assert result["escalated"] is True
    ctx.task_manager.escalate_task.assert_called_once_with(
        ctx.task_id,
        reason="needs_human:pr_delivery:human decision required",
    )
    ctx.task_manager.stage_states.approve_review.assert_not_called()
    ctx.task_manager.stage_states.reject_review.assert_not_called()


def test_rejected_under_cap_returns_to_ready(temp_db, sample_project) -> None:
    task = _pr_task_in_review(temp_db, sample_project, max_review_rounds=2)

    result = _record_pr_verdict(_real_context(temp_db))(
        task_id=task.id,
        verdict="request_changes",
        findings="needs another pass",
    )

    row = stage_row(temp_db, task.id, "pr")
    task_state = task_row(temp_db, task.id)
    assert result["stage"]["state"] == "ready"
    assert row["state"] == "ready"
    assert row["review_round_count"] == 1
    assert task_state["closed_at"] is None
    assert task_state["is_escalated"] == 0


def test_rejected_over_cap_escalates(temp_db, sample_project) -> None:
    task = _pr_task_in_review(temp_db, sample_project, max_review_rounds=1)

    _record_pr_verdict(_real_context(temp_db))(
        task_id=task.id,
        verdict="request_changes",
        findings="still blocked",
    )

    row = stage_row(temp_db, task.id, "pr")
    task_state = task_row(temp_db, task.id)
    assert row["state"] == "ready"
    assert row["review_round_count"] == 1
    assert task_state["is_escalated"] == 1
    assert task_state["escalated_at"] is not None


def test_per_stage_max_review_rounds_override_works(temp_db, sample_project) -> None:
    task = _pr_task_in_review(
        temp_db,
        sample_project,
        max_review_rounds=2,
        review_round_count=1,
    )

    _record_pr_verdict(_real_context(temp_db))(
        task_id=task.id,
        verdict="request_changes",
        findings="second failed review",
    )

    task_state = task_row(temp_db, task.id)
    row = stage_row(temp_db, task.id, "pr")
    assert row["review_round_count"] == 2
    assert task_state["is_escalated"] == 1
    assert task_state["escalated_at"] is not None


register_contract_tests(
    globals(),
    {
        "test_approved_calls_approve_review": ("approved PR verdict delegates to approve_review"),
        "test_approved_calls_approve_review_only": (
            "approved PR verdict calls approve_review and no other stage mutator"
        ),
        "test_approved_does_not_advance_to_merge": (
            "approved PR verdict leaves advancement to the dispatcher"
        ),
        "test_approved_does_not_call_complete_stage": (
            "approved PR verdict does not call complete_stage"
        ),
        "test_approved_increments_no_counter": "approved PR verdict increments no counters",
        "test_approved_post_state_is_review_approved": (
            "approved PR verdict leaves the pr row in review_approved"
        ),
        "test_request_changes_treated_as_rejected": (
            "request_changes PR verdict is equivalent to reject_review"
        ),
        "test_raises_when_pr_not_in_needs_review": (
            "record_pr_verdict raises if the pr row is not needs_review"
        ),
        "test_rejected_calls_reject_review_only": (
            "rejected PR verdict calls reject_review and no work-attempt mutator"
        ),
        "test_rejected_increments_review_round_count": (
            "rejected PR verdict increments review_round_count only"
        ),
    },
    required_symbols=("gobby.mcp_proxy.tools.tasks._stage_ops:create_stage_ops_registry",),
)
