"""Phase 2 red contracts for record_pr_verdict MCP tool."""

from __future__ import annotations

import json
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import gobby.mcp_proxy.tools.tasks._stage_ops as stage_ops
from tests.phase2_stage_contract_helpers import register_contract_tests

pytestmark = pytest.mark.unit


def _context() -> SimpleNamespace:
    executed: list[tuple[str, tuple[object, ...]]] = []
    db = SimpleNamespace(
        executed=executed,
        transaction=lambda: nullcontext(
            SimpleNamespace(execute=lambda sql, params: executed.append((sql, params)))
        ),
    )
    stage_states = SimpleNamespace(
        approve_review=Mock(return_value=SimpleNamespace(stage_name="pr", state="review_approved")),
        reject_review=Mock(return_value=SimpleNamespace(stage_name="pr", state="ready")),
        complete_stage=Mock(side_effect=AssertionError("record_pr_verdict must not advance")),
    )
    return SimpleNamespace(
        task_manager=SimpleNamespace(
            db=db,
            stage_states=stage_states,
            get_task=Mock(return_value=SimpleNamespace(id="task-1")),
        ),
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


def test_approved_calls_approve_review_no_advance(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_stage_view(monkeypatch)
    ctx = _context()

    result = _record_pr_verdict(ctx)(
        task_id="task-1",
        verdict="approved",
        findings="looks good",
        report_ref="pr-review.md",
    )

    assert result["stage"] == {"stage_name": "pr", "state": "review_approved"}
    ctx.task_manager.stage_states.approve_review.assert_called_once_with(
        "task-1",
        "pr",
        by_session_id=None,
        notes="looks good",
    )
    ctx.task_manager.stage_states.complete_stage.assert_not_called()


def test_approved_writes_artifacts(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_stage_view(monkeypatch)
    ctx = _context()

    _record_pr_verdict(ctx)(
        task_id="task-1",
        verdict="approved",
        findings="looks good",
        report_ref="pr-review.md",
    )

    sql, params = ctx.task_manager.db.executed[0]
    payload = json.loads(params[1])
    assert "structured_pr_verdict" in sql
    assert "pr_review_report" in sql
    assert payload == {
        "verdict": "approved",
        "findings": "looks good",
        "report_ref": "pr-review.md",
    }
    assert params[2] == "pr-review.md"


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
        "test_needs_changes_treated_as_rejected": (
            "needs_changes PR verdict is equivalent to reject_review"
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
