"""Oversized task-close review gate routing tests."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, MagicMock

import pytest

import gobby.mcp_proxy.tools.tasks._lifecycle_review_gate as review_gate
from gobby.mcp_proxy.tools.tasks._context import RegistryContext
from gobby.mcp_proxy.tools.tasks._lifecycle_review_gate import (
    SubmittedCloseReview,
    evaluate_close_criteria,
)
from gobby.mcp_proxy.tools.tasks._lifecycle_validation import ValidationResult
from gobby.storage.tasks import Task
from gobby.tasks.validation import PreparedCloseReview, TaskValidator

pytestmark = pytest.mark.unit


@pytest.mark.asyncio
async def test_oversized_review_requires_internal_background_review(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        review_gate, "evaluate_criteria_review", AsyncMock(return_value=_oversized())
    )

    result = await _evaluate()

    assert result.error_type == "agentic_review_required"
    assert result.extra["review_fingerprint"] == "close"
    assert result.extra["deterministic_evidence_fingerprint"] == "evidence"
    assert "spawn_request" not in result.extra
    assert "review_run_id" not in result.extra


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["valid", "invalid"])
async def test_matching_submitted_verdict_uses_shared_accounting(
    monkeypatch: pytest.MonkeyPatch,
    status: str,
) -> None:
    accounted = ValidationResult(can_close=status == "valid")
    account = MagicMock(return_value=accounted)
    monkeypatch.setattr(review_gate, "account_criteria_verdict", account)

    result = await _evaluate(
        submitted=SubmittedCloseReview(
            verdict={
                "status": status,
                "criteria": [{"index": 1, "satisfied": status == "valid", "gap": None}],
                "feedback": status,
            },
            review_fingerprint="close",
            evidence_fingerprint="evidence",
        )
    )

    assert result is accounted
    account.assert_called_once()
    assert account.call_args.kwargs["verdict"].status == status
    assert result.extra["deterministic_evidence_fingerprint"] == "evidence"


@pytest.mark.asyncio
async def test_stale_submitted_fingerprint_skips_accounting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    account = MagicMock()
    monkeypatch.setattr(review_gate, "account_criteria_verdict", account)

    result = await _evaluate(
        submitted=SubmittedCloseReview(
            verdict={"status": "valid", "criteria": [], "feedback": "ok"},
            review_fingerprint="stale",
            evidence_fingerprint="evidence",
        )
    )

    assert result.error_type == "agentic_review_stale"
    account.assert_not_called()


@pytest.mark.asyncio
async def test_malformed_submitted_verdict_can_be_corrected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    account = MagicMock()
    monkeypatch.setattr(review_gate, "account_criteria_verdict", account)

    result = await _evaluate(
        submitted=SubmittedCloseReview(
            verdict={"status": "unknown", "criteria": [], "feedback": "invalid status"},
            review_fingerprint="close",
            evidence_fingerprint="evidence",
        )
    )

    assert result.error_type == "agentic_review_malformed"
    account.assert_not_called()


def _oversized() -> ValidationResult:
    return ValidationResult(
        can_close=False,
        error_type="validation_prompt_too_large",
        message="large",
        extra={
            "review_fingerprint": "close",
            "evidence_fingerprint": "evidence",
            "prompt_chars": 256_001,
            "prompt_limit": 256_000,
        },
    )


def _prepared() -> PreparedCloseReview:
    return PreparedCloseReview(
        prompt="prompt",
        criteria=("Criterion.",),
        prompt_chars=256_001,
        prompt_limit=256_000,
        review_fingerprint="close",
        evidence_fingerprint="evidence",
        manifest_count=1,
        excerpt_chars=10,
    )


async def _evaluate(
    *,
    submitted: SubmittedCloseReview | None = None,
) -> ValidationResult:
    task = Task(
        id="task",
        project_id="project",
        title="Task",
        priority=2,
        task_type="task",
        created_at=datetime(2026, 8, 21, tzinfo=UTC),
        updated_at=datetime(2026, 8, 21, tzinfo=UTC),
        validation_criteria="Criterion.",
    )
    validator = cast(
        TaskValidator,
        SimpleNamespace(prepare_task_review=MagicMock(return_value=_prepared())),
    )
    ctx = cast(RegistryContext, SimpleNamespace())
    return await evaluate_close_criteria(
        task=task,
        task_validator=validator,
        ctx=ctx,
        resolved_id=task.id,
        changes_summary="summary",
        diff_text="diff",
        checklist_facts={},
        validation_config=None,
        reason="completed",
        description="",
        test_bodies="tests",
        submitted_review=submitted,
    )
